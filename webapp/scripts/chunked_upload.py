#!/usr/bin/env python3
"""
Digitises a large PDF a few pages at a time, writing straight into AjanDB's
SQLite database instead of going through the web server's /api/upload.

Why this exists alongside the web upload: a big scanned book can take many
minutes of OCR, and even the async-job/polling design behind /api/upload
still needs a browser tab (and, if you're behind a tunnel like cloudflared,
the tunnel itself) to stay reachable for that whole time. This script skips
the web layer entirely - point it at a PDF and a database file, and it pulls
out one page-range sub-PDF at a time, converts each range with the same
backend/convert.py pipeline the web app uses, and inserts the resulting
chunks as soon as that range is done. If it's interrupted partway through a
long book, --resume picks up after the last range it finished instead of
reprocessing the whole thing; everything already inserted is immediately
searchable in the running app in the meantime.

Known sharp edge: state is only recorded *after* a range's chunks are
successfully inserted, so a crash between the DB insert and the state-file
write (rare, but possible) would re-insert that one range's chunks as
duplicates on the next --resume run. Not worth guarding against for a
single-user personal tool; if it ever matters, delete the duplicate rows by
id (see /api/documents) or drop and re-run without --resume.

Usage:
    python3 scripts/chunked_upload.py mybook.pdf
    python3 scripts/chunked_upload.py mybook.pdf --chunk-size 10 --resume
    python3 scripts/chunked_upload.py mybook.pdf --engine vision --provider anthropic --api-key sk-ant-...
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # webapp/

import pymupdf as fitz  # noqa: E402

from backend import categorize, convert, db  # noqa: E402

STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chunked_upload_state"
)


def _state_path(pdf_path: str) -> str:
    # Keyed by the absolute path's hash (not just the basename) so two
    # different files that happen to share a name don't collide.
    key = hashlib.sha1(os.path.abspath(pdf_path).encode()).hexdigest()[:16]
    return os.path.join(STATE_DIR, f"{os.path.basename(pdf_path)}.{key}.json")


def _load_state(pdf_path: str) -> dict:
    path = _state_path(pdf_path)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"done_ranges": []}


def _save_state(pdf_path: str, state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_state_path(pdf_path), "w") as f:
        json.dump(state, f)


def _page_ranges(total_pages: int, chunk_size: int) -> list[tuple[int, int]]:
    return [(start, min(start + chunk_size, total_pages)) for start in range(0, total_pages, chunk_size)]


def _extract_page_range(pdf_path: str, start: int, end: int) -> str:
    """Writes 0-indexed pages [start, end) out to a new temp PDF, returns its path."""
    src = fitz.open(pdf_path)
    sub = fitz.open()
    sub.insert_pdf(src, from_page=start, to_page=end - 1)
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    sub.save(tmp_path)
    sub.close()
    src.close()
    return tmp_path


def process_pdf(pdf_path: str, db_path: str = None, chunk_size: int = 5,
                 engine: str = "classical", provider: str = None, model: str = None,
                 langs: str = None, category: str = None, api_key: str = None,
                 resume: bool = False, stop_on_error: bool = False, progress=None) -> dict:
    """Converts and stores `pdf_path` a `chunk_size`-page range at a time.

    `progress`, if given, is called with a one-line status string after each
    range - the CLI uses it to print; tests can capture it into a list.

    Returns {"total_pages", "ranges_done", "ranges_failed", "chunk_ids"}.
    Safe to interrupt and re-run with resume=True: ranges already recorded
    in the on-disk state file (see STATE_DIR) are skipped."""
    db_path = db_path or db.DEFAULT_DB_PATH
    filename = os.path.basename(pdf_path)

    src = fitz.open(pdf_path)
    total_pages = src.page_count
    src.close()

    ranges = _page_ranges(total_pages, chunk_size)
    state = _load_state(pdf_path) if resume else {"done_ranges": []}
    done_ranges = {tuple(r) for r in state["done_ranges"]}

    conn = db.get_connection(db_path)
    resolved_category = (category or "").strip() or None
    if resolved_category is None:
        # Resuming (or re-running) an already-started book should keep using
        # whatever category its earlier chunks got, rather than suggesting a
        # fresh one from whatever page range happens to run first this time.
        existing = conn.execute(
            "SELECT category FROM documents WHERE source_filename = ? LIMIT 1", (filename,)
        ).fetchone()
        if existing:
            resolved_category = existing["category"]

    chunk_ids: list[int] = []
    ranges_failed: list[tuple[int, int, str]] = []

    try:
        for start, end in ranges:
            if (start, end) in done_ranges:
                if progress:
                    progress(f"Skipping pages {start + 1}-{end} (already done)")
                continue

            tmp_pdf = _extract_page_range(pdf_path, start, end)
            try:
                if engine == "vision":
                    chunks = convert.pdf_to_markdown_vision(
                        tmp_pdf, provider=provider, model=model, api_key=api_key)
                elif engine == "classical":
                    chunks = convert.pdf_to_markdown(tmp_pdf, langs=langs)
                else:
                    raise ValueError(f"Unknown conversion engine {engine!r} - expected 'classical' or 'vision'")
            except Exception as e:
                ranges_failed.append((start, end, str(e)))
                if progress:
                    progress(f"FAILED pages {start + 1}-{end}: {e}")
                if stop_on_error:
                    raise
                continue
            finally:
                os.unlink(tmp_pdf)

            for chunk in chunks:
                chunk["page_number"] += start

            if resolved_category is None:
                sample_text = "\n\n".join(c["markdown"] for c in chunks[:2])
                resolved_category = categorize.suggest_category(
                    sample_text, provider=provider, model=model, api_key=api_key) or "Uncategorized"

            ids = db.insert_chunks(
                conn, filename, "pdf", resolved_category, chunks, total_pages=total_pages)
            chunk_ids.extend(ids)

            done_ranges.add((start, end))
            state["done_ranges"] = sorted(done_ranges)
            _save_state(pdf_path, state)

            if progress:
                progress(f"Stored pages {start + 1}-{end} ({len(ids)} chunk(s))")
    finally:
        conn.close()

    return {
        "total_pages": total_pages,
        "ranges_done": sorted(done_ranges),
        "ranges_failed": ranges_failed,
        "chunk_ids": chunk_ids,
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf_path")
    parser.add_argument("--chunk-size", type=int, default=5, help="pages per batch (default: 5)")
    parser.add_argument("--engine", choices=["classical", "vision"], default="classical")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default=None,
                         help="vision engine / category suggestion only (default: anthropic)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--langs", default=None,
                         help="Tesseract language codes, e.g. eng+tha (classical engine only)")
    parser.add_argument("--category", default=None,
                         help="skip auto-suggestion and use this category for every chunk")
    parser.add_argument("--api-key", default=None,
                         help="API key for the vision engine / category suggestion "
                              "(or set ANTHROPIC_API_KEY/OPENAI_API_KEY in the environment)")
    parser.add_argument("--db-path", default=db.DEFAULT_DB_PATH)
    parser.add_argument("--resume", action="store_true",
                         help="skip page ranges already stored from a previous run of this script")
    parser.add_argument("--stop-on-error", action="store_true",
                         help="abort on the first failed range instead of continuing past it")
    args = parser.parse_args()

    if not os.path.exists(args.pdf_path):
        parser.error(f"No such file: {args.pdf_path}")

    result = process_pdf(
        args.pdf_path, db_path=args.db_path, chunk_size=args.chunk_size, engine=args.engine,
        provider=args.provider, model=args.model, langs=args.langs, category=args.category,
        api_key=args.api_key, resume=args.resume, stop_on_error=args.stop_on_error,
        progress=print)

    print(f"\nDone: {len(result['ranges_done'])} range(s), {len(result['chunk_ids'])} chunk(s) stored, "
          f"{result['total_pages']} total page(s).")
    if result["ranges_failed"]:
        print(f"{len(result['ranges_failed'])} range(s) failed:")
        for start, end, err in result["ranges_failed"]:
            print(f"  pages {start + 1}-{end}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
