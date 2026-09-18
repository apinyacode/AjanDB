"""
FastAPI backend for AjanDB: upload unstructured files -> markdown in SQLite,
keyword search over stored files, and a chatbot endpoint that compiles a
book from whatever matches a topic.

Run with:
    uvicorn backend.main:app --reload --app-dir webapp
"""
import os
import re
import shutil
import tempfile
import threading
import uuid
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import book_compiler, categorize, convert, db, review

app = FastAPI(title="AjanDB")

_FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)


@app.get("/images/{filename}")
def get_image(filename: str):
    """Serves an extracted image (see convert.py's _save_image_as_jpg) -
    a plain route reading convert.IMAGES_DIR fresh on every request rather
    than a StaticFiles mount, which would otherwise bake in whatever
    directory existed at import time and not follow along if that ever
    changes (as it does in tests, via monkeypatch). filename is reduced to
    its basename first so a malformed value can't escape IMAGES_DIR."""
    path = os.path.join(convert.IMAGES_DIR, os.path.basename(filename))
    if not os.path.isfile(path):
        raise HTTPException(404, "Image not found")
    return FileResponse(path, media_type="image/jpeg")


def _resolve_client_key(provider: str | None, anthropic_key: str | None,
                         openai_key: str | None) -> str | None:
    """Picks whichever browser-supplied key matches `provider` (defaulting
    to "anthropic", same default every provider-aware module here uses).
    Returns None when the matching key wasn't supplied - callers then fall
    back to the backend's own environment variables, unchanged."""
    resolved_provider = (provider or "anthropic").lower()
    return openai_key if resolved_provider == "openai" else anthropic_key


# --- Upload jobs ---
#
# OCR (classical or vision-LLM) can take anywhere from milliseconds to many
# minutes per document, and this app is typically reached through a tunnel
# (cloudflared, ngrok, ...) or a flaky mobile connection - neither promises
# a single HTTP request stays alive that long. A request that does get cut
# mid-flight shows up client-side as "<!DOCTYPE ...> is not valid JSON" (the
# tunnel/proxy's own error page instead of our JSON), which is exactly what
# happened here: real conversion work was still running when the connection
# was cancelled after several minutes.
#
# So /api/upload does no OCR itself: it saves the file, hands the actual
# work to a background thread, and returns a job id immediately. The
# frontend polls /api/upload/{job_id} - each poll is a fast, cheap request,
# so nothing depends on one connection surviving the whole conversion.
# In-memory (not persisted) is fine here: single-process app, jobs are
# short-lived, and a restart mid-job would have lost that upload anyway.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Log lines per job, kept separate from `_jobs` (which _set_job overwrites
# wholesale on every status change) so the running "converting / saved"
# history survives from "processing" through to "done"/"error" - the
# frontend polls this alongside status to render a live console instead of
# just a single current-status line.
_job_logs: dict[str, list[str]] = {}


def _set_job(job_id: str, **fields):
    with _jobs_lock:
        _jobs[job_id] = fields


def _upload_log(job_id: str, message: str):
    print(f"[upload {job_id[:8]}] {message}", flush=True)
    with _jobs_lock:
        _job_logs.setdefault(job_id, []).append(message)


def _run_upload_job(job_id: str, tmp_path: str, filename: str, ext: str,
                     engine: str, provider: str | None, model: str | None,
                     category: str | None, anthropic_api_key: str | None,
                     openai_api_key: str | None):
    vision_key = _resolve_client_key(provider, anthropic_api_key, openai_api_key)
    _upload_log(job_id, f"Starting: {filename} (engine={engine}, provider={provider or 'default'})")

    def _progress(page_number: int, total_pages: int):
        _upload_log(job_id, f"Converting page {page_number + 1}/{total_pages}...")

    try:
        chunks = convert.convert_to_markdown(
            tmp_path, filename, engine=engine, provider=provider, model=model,
            api_key=vision_key, progress=_progress)
    except ValueError as e:
        _upload_log(job_id, f"ERROR: {e}")
        _set_job(job_id, status="error", code=400, detail=str(e))
        return
    except RuntimeError as e:
        _upload_log(job_id, f"ERROR: {e}")
        _set_job(job_id, status="error", code=503, detail=str(e))
        return
    except Exception as e:
        _upload_log(job_id, f"ERROR: Failed to convert {filename}: {e}")
        _set_job(job_id, status="error", code=422, detail=f"Failed to convert {filename}: {e}")
        return
    finally:
        os.unlink(tmp_path)

    _upload_log(job_id, f"Converted {filename}: {len(chunks)} chunk(s)")

    resolved_category = (category or "").strip()
    if not resolved_category:
        sample_text = "\n\n".join(c["markdown"] for c in chunks[:2])
        if anthropic_api_key:
            resolved_category = categorize.suggest_category(
                sample_text, provider="anthropic", api_key=anthropic_api_key) or "Uncategorized"
        elif openai_api_key:
            resolved_category = categorize.suggest_category(
                sample_text, provider="openai", api_key=openai_api_key) or "Uncategorized"
        else:
            resolved_category = categorize.suggest_category(
                sample_text, provider=provider, model=model) or "Uncategorized"

    source_type = ext.lstrip(".")
    conn = db.get_connection()  # own connection - sqlite3 connections aren't shared across threads
    try:
        chunk_ids = db.insert_chunks(conn, filename, source_type, resolved_category, chunks)
    finally:
        conn.close()

    _upload_log(job_id, f"Done: saved {len(chunk_ids)} chunk(s), category '{resolved_category}'")
    _set_job(
        job_id, status="done",
        result={
            "source_filename": filename,
            "source_type": source_type,
            "category": resolved_category,
            "chunks": [{"id": cid, **chunk} for cid, chunk in zip(chunk_ids, chunks)],
        },
    )


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    engine: str = Form("classical"),
    provider: str | None = Form(None),
    model: str | None = Form(None),
    category: str | None = Form(None),
    anthropic_api_key: str | None = Form(None),
    openai_api_key: str | None = Form(None),
):
    """`engine`="vision" routes scanned PDF pages through a per-page LLM call
    (`provider`: "anthropic" or "openai") instead of local Tesseract - see
    convert.pdf_to_markdown_vision. Needs the matching API key, either typed
    into the frontend (sent here as `anthropic_api_key`/`openai_api_key` -
    stored only in the browser, never persisted server-side) or set as
    ANTHROPIC_API_KEY/OPENAI_API_KEY in the backend's environment; neither
    is the same as a claude.ai/ChatGPT Plus subscription.

    `category` is optional - leave it blank to have it auto-suggested from
    the converted content using whichever key is available (falls back to
    "Uncategorized" if none is; that's never a hard failure).

    Returns immediately with a job id - the actual conversion (which can
    take a long time for scanned PDFs) runs in the background. Poll
    GET /api/upload/{job_id} for the result."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in convert.SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext or '(none)'}")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    job_id = uuid.uuid4().hex
    _set_job(job_id, status="processing")
    threading.Thread(
        target=_run_upload_job,
        args=(job_id, tmp_path, file.filename, ext, engine, provider, model,
              category, anthropic_api_key, openai_api_key),
        daemon=True,
    ).start()

    return {"job_id": job_id, "status": "processing"}


@app.get("/api/upload/{job_id}")
def upload_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        log = list(_job_logs.get(job_id, []))
    if not job:
        raise HTTPException(404, "Unknown upload job")
    if job["status"] == "processing":
        return {"status": "processing", "log": log}
    if job["status"] == "error":
        raise HTTPException(job["code"], job["detail"])
    return {"status": "done", "log": log, **job["result"]}


class ReviewApproveRequest(BaseModel):
    markdown: str | None = None  # a human edit to save instead of the converted text, if given
    needs_review: bool | None = None  # override the auto-computed flag, if given


@app.post("/api/review/start")
async def review_start(
    file: UploadFile = File(...),
    engine: str = Form("classical"),
    provider: str | None = Form(None),
    model: str | None = Form(None),
    langs: str | None = Form(None),
    category: str | None = Form(None),
    anthropic_api_key: str | None = Form(None),
    openai_api_key: str | None = Form(None),
):
    """Starts a page-by-page review session: converts and returns page 1
    immediately, alongside a rendered preview image, so the frontend can
    show scan-vs-markdown side by side and let a human approve (optionally
    after editing) or skip each page before it's saved - see
    backend/review.py for the full flow. PDF only; other file types have no
    OCR step for a human to check page-by-page."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext != ".pdf":
        raise HTTPException(400, "Page-by-page review is only supported for PDFs")
    if engine not in ("classical", "vision"):
        raise HTTPException(400, f"Unknown conversion engine {engine!r} - expected 'classical' or 'vision'")

    api_key = _resolve_client_key(provider, anthropic_api_key, openai_api_key)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        return review.start(
            tmp_path, file.filename, engine=engine, provider=provider, model=model,
            langs=langs, category=category, api_key=api_key)
    except ValueError as e:
        os.unlink(tmp_path)
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        os.unlink(tmp_path)
        raise HTTPException(503, str(e))
    except Exception as e:
        os.unlink(tmp_path)
        raise HTTPException(422, f"Failed to convert page 1 of {file.filename}: {e}")


@app.post("/api/review/{session_id}/approve")
def review_approve(session_id: str, req: ReviewApproveRequest):
    try:
        return review.approve(session_id, markdown=req.markdown, needs_review=req.needs_review)
    except KeyError:
        raise HTTPException(404, "Unknown review session")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(422, f"Failed to convert the next page: {e}")


@app.post("/api/review/{session_id}/skip")
def review_skip(session_id: str):
    try:
        return review.skip(session_id)
    except KeyError:
        raise HTTPException(404, "Unknown review session")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(422, f"Failed to convert the next page: {e}")


@app.post("/api/review/{session_id}/retry")
def review_retry(session_id: str):
    """Re-attempts converting the page after the last approve()/skip() call -
    use this if that request failed (e.g. a transient vision-LLM API error)
    instead of resubmitting approve()/skip(), which would reject the retry
    as there being nothing currently pending."""
    try:
        return review.retry(session_id)
    except KeyError:
        raise HTTPException(404, "Unknown review session")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(422, f"Failed to convert the next page: {e}")


class VerifyRequest(BaseModel):
    provider: str | None = None  # which vision-LLM to cross-check against - "anthropic" (default) or "openai"
    model: str | None = None
    anthropic_api_key: str | None = None  # typed into the frontend - stored only in the browser
    openai_api_key: str | None = None


@app.post("/api/review/{session_id}/verify")
def review_verify(session_id: str, req: VerifyRequest):
    """Cross-checks the page currently awaiting review against a second
    vision-LLM call, independent of whatever engine/provider produced the
    pending transcription, and returns a word-level diff plus an agreement
    percentage - see review.verify_second_opinion for why this is a more
    trustworthy signal than either model's own confidence score. Doesn't
    touch approve/skip/save state; safe to call more than once."""
    api_key = _resolve_client_key(req.provider, req.anthropic_api_key, req.openai_api_key)
    try:
        return review.verify_second_opinion(
            session_id, provider=req.provider, model=req.model, api_key=api_key)
    except KeyError:
        raise HTTPException(404, "Unknown review session")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(422, f"Failed to get a second opinion: {e}")


@app.post("/api/review/{session_id}/cancel")
def review_cancel(session_id: str):
    try:
        return review.cancel(session_id)
    except KeyError:
        raise HTTPException(404, "Unknown review session")


@app.get("/api/documents")
def list_documents():
    conn = db.get_connection()
    try:
        return db.list_documents(conn)
    finally:
        conn.close()


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int):
    conn = db.get_connection()
    try:
        doc = db.get_document(conn, doc_id)
    finally:
        conn.close()
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


@app.get("/api/books")
def list_books():
    """One entry per uploaded source file (whether it needed OCR or was
    stored as-is), for browsing the library rather than searching it - see
    db.list_books()'s docstring for how "one book" is identified."""
    conn = db.get_connection()
    try:
        return db.list_books(conn)
    finally:
        conn.close()


@app.get("/api/books/{book_id}")
def get_book(book_id: int):
    conn = db.get_connection()
    try:
        chunks = db.get_book_chunks(conn, book_id)
    finally:
        conn.close()
    if not chunks:
        raise HTTPException(404, "Book not found")
    first = chunks[0]
    return {
        "source_filename": first["source_filename"],
        "source_type": first["source_type"],
        "total_pages": first["total_pages"],
        "category": first["category"],
        "chunks": chunks,
    }


def _book_to_markdown(chunks: list[dict]) -> str:
    first = chunks[0]
    lines = [
        f"# {first['source_filename']}", "",
        f"*Category: {first['category']} · {first['total_pages']} page(s)*", "",
    ]
    for chunk in chunks:
        lines.append(f"## Page {chunk['page_number']}")
        lines.append("")
        lines.append(chunk["markdown"])
        lines.append("")
    return "\n".join(lines)


def _export_filename(source_filename: str) -> str:
    base = os.path.splitext(source_filename)[0]
    return f"{base}.md"


def _content_disposition(filename: str) -> str:
    # A non-ASCII filename (very possible here - e.g. a Thai book title)
    # can't go in the plain `filename=` parameter, so it's UTF-8
    # percent-encoded per RFC 5987 in `filename*=`, with an ASCII-only
    # fallback in `filename=` for anything that doesn't support that.
    ascii_fallback = re.sub(r'[^\x20-\x7E]', "_", filename).replace('"', "'") or "export.md"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


_IMAGE_LINK_RE = re.compile(r"(!\[[^\]]*\]\()(/images/[^)]+)(\))")


def _absolutize_image_links(markdown: str, request: Request) -> str:
    """Stored markdown references images by a host-relative /images/<hash>.jpg
    path (see convert.py), which resolves correctly wherever the app itself
    is being browsed from - but a downloaded .md file has no "current page"
    to resolve a relative link against, so exporting rewrites each one to a
    full URL against whatever host served this request (localhost, a
    cloudflared tunnel, ...). Opening the export elsewhere, or after this
    server has stopped running, will show broken image links - there's no
    way around that without bundling the image files with the export too."""
    base = str(request.base_url).rstrip("/")
    return _IMAGE_LINK_RE.sub(lambda m: f"{m.group(1)}{base}{m.group(2)}{m.group(3)}", markdown)


@app.get("/api/books/{book_id}/export")
def export_book(book_id: int, request: Request):
    """Downloads one book's full chunk set as a single concatenated
    markdown file, in page order - the only way any converted text leaves
    the database as an actual file (it's otherwise stored purely as text
    in SQLite, never written to disk - see the README's "Where is the
    output .md saved?" note)."""
    conn = db.get_connection()
    try:
        chunks = db.get_book_chunks(conn, book_id)
    finally:
        conn.close()
    if not chunks:
        raise HTTPException(404, "Book not found")
    markdown = _absolutize_image_links(_book_to_markdown(chunks), request)
    filename = _export_filename(chunks[0]["source_filename"])
    return Response(
        content=markdown, media_type="text/markdown",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@app.get("/api/search")
def search(q: str):
    conn = db.get_connection()
    try:
        return db.search(conn, q)
    finally:
        conn.close()


class ChatRequest(BaseModel):
    instruction: str
    provider: str | None = None  # "anthropic" (default) or "openai" - picks which API key is used
    model: str | None = None
    anthropic_api_key: str | None = None  # typed into the frontend - stored only in the browser
    openai_api_key: str | None = None


@app.post("/api/chat")
def chat(req: ChatRequest):
    conn = db.get_connection()
    api_key = _resolve_client_key(req.provider, req.anthropic_api_key, req.openai_api_key)
    try:
        return book_compiler.compile_book(
            req.instruction, conn, provider=req.provider, model=req.model, api_key=api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Book compilation failed: {e}")
    finally:
        conn.close()


# Serve the frontend last so it doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
