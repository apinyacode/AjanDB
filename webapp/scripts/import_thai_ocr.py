#!/usr/bin/env python3
"""
Imports pages already transcribed by the standalone thai_ocr scripts
(FarmDeApp repo, thai_ocr/) into the QnA tab - so pages that were already
paid for aren't sent to Claude again, and any review done in the text files
(edits, and pages renamed to _DONE) carries over.

Reads each document folder under thai_ocr's work/ directory:
    pages/page_001.jpg            full page
    review/page_001.jpg           cropped handwriting   (older layout: crops/page_001.jpg)
    review/page_001_REVIEW.txt    reviewed text; _DONE in the name -> status 'done'
    ai/page_001.json              Claude's original answer (used when there's no .txt)

Pages with neither a .txt nor a .json come in as 'pending' - "Retry
waiting/failed pages" in the QnA tab transcribes them. A document whose name
is already in the QnA tab is skipped, so running this twice doesn't duplicate.

Usage:
    python3 scripts/import_thai_ocr.py ~/thai_ocr/work
    python3 scripts/import_thai_ocr.py ~/thai_ocr/work --db data/ajandb.sqlite3
"""
import argparse
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # webapp/

from backend import db, qna  # noqa: E402

REVIEW_FILE = re.compile(r"^page_(\d+)_([A-Za-z]+)\.txt$")
SECTIONS = {
    "== TEXT (same line breaks as the paper) ==": "text",
    "== CLEAN VERSION (one paragraph) ==": "clean",
    "== SPELLING FIXES (as written -> corrected) ==": "fixes",
    "== NOTES ==": "notes",
}


def parse_review_text(text: str) -> dict:
    parts = {key: [] for key in SECTIONS.values()}
    current = None
    for line in text.splitlines():
        if line.startswith("#"):
            continue  # thai_ocr's help header
        if line.strip() in SECTIONS:
            current = SECTIONS[line.strip()]
        elif current:
            parts[current].append(line.rstrip())
    return {key: "\n".join(lines).strip() for key, lines in parts.items()}


def import_document(conn, doc_dir: str) -> str:
    name = os.path.basename(doc_dir.rstrip("/"))
    source_filename = f"{name}.pdf"
    if any(d["source_filename"] == source_filename for d in db.list_qna_documents(conn)):
        return f"{name}: already in QnA - skipped"

    pages_dir = os.path.join(doc_dir, "pages")
    page_numbers = sorted(int(f[5:8]) for f in os.listdir(pages_dir) if re.fullmatch(r"page_\d{3}\.jpg", f))
    if not page_numbers:
        return f"{name}: no pages/ images - skipped"
    texts = {}
    review_dir = os.path.join(doc_dir, "review")
    if os.path.isdir(review_dir):
        for f in os.listdir(review_dir):
            match = REVIEW_FILE.match(f)
            if match:
                texts[int(match.group(1))] = (os.path.join(review_dir, f), match.group(2).upper())

    document_id = db.create_qna_document(conn, source_filename, len(page_numbers))
    os.makedirs(os.path.join(qna.QNA_DIR, str(document_id)), exist_ok=True)
    counts = {"done": 0, "review": 0, "pending": 0}
    for n in page_numbers:
        crop = next((p for p in (os.path.join(doc_dir, "review", f"page_{n:03d}.jpg"),
                                 os.path.join(doc_dir, "crops", f"page_{n:03d}.jpg")) if os.path.isfile(p)), None)
        shutil.copyfile(os.path.join(pages_dir, f"page_{n:03d}.jpg"), qna.image_path(document_id, n, "full"))
        shutil.copyfile(crop or os.path.join(pages_dir, f"page_{n:03d}.jpg"), qna.image_path(document_id, n, "crop"))

        ai_path = os.path.join(doc_dir, "ai", f"page_{n:03d}.json")
        ai = json.load(open(ai_path, encoding="utf-8")) if os.path.isfile(ai_path) else None
        if n in texts:
            path, status_word = texts[n]
            fields = parse_review_text(open(path, encoding="utf-8").read())
            status = "done" if status_word == "DONE" else "review"
        elif ai:
            fields, status = qna.to_fields(ai), "review"
        else:
            counts["pending"] += 1
            continue
        fields.pop("confidence", None)
        db.save_qna_transcription(conn, document_id, n, confidence=(ai or {}).get("confidence", ""),
                                  ai_json=json.dumps(ai, ensure_ascii=False) if ai else None,
                                  status=status, **fields)
        counts[status] += 1
    return (f"{name}: imported {len(page_numbers)} pages "
            f"({counts['review']} to review, {counts['done']} done, {counts['pending']} waiting)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("work_dir", help="thai_ocr's work/ folder (one sub-folder per PDF)")
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH, help="AjanDB database file")
    args = parser.parse_args()

    conn = db.get_connection(args.db)
    try:
        for entry in sorted(os.listdir(args.work_dir)):
            doc_dir = os.path.join(args.work_dir, entry)
            if os.path.isdir(os.path.join(doc_dir, "pages")):
                print(import_document(conn, doc_dir))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
