"""
FastAPI backend for AjanDB: upload unstructured files -> markdown in SQLite,
keyword search over stored files, and a chatbot endpoint that compiles a
book from whatever matches a topic.

Run with:
    uvicorn backend.main:app --reload --app-dir webapp
"""
import os
import shutil
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import book_compiler, categorize, convert, db

app = FastAPI(title="AjanDB")

_FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)


def _resolve_client_key(provider: str | None, anthropic_key: str | None,
                         openai_key: str | None) -> str | None:
    """Picks whichever browser-supplied key matches `provider` (defaulting
    to "anthropic", same default every provider-aware module here uses).
    Returns None when the matching key wasn't supplied - callers then fall
    back to the backend's own environment variables, unchanged."""
    resolved_provider = (provider or "anthropic").lower()
    return openai_key if resolved_provider == "openai" else anthropic_key


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

    The file is chunked to roughly one page per row (see convert.py), each
    carrying its own OCR confidence and a needs_review flag."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in convert.SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext or '(none)'}")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    vision_key = _resolve_client_key(provider, anthropic_api_key, openai_api_key)
    try:
        chunks = convert.convert_to_markdown(
            tmp_path, file.filename, engine=engine, provider=provider, model=model,
            api_key=vision_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(422, f"Failed to convert {file.filename}: {e}")
    finally:
        os.unlink(tmp_path)

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
    conn = db.get_connection()
    try:
        chunk_ids = db.insert_chunks(conn, file.filename, source_type, resolved_category, chunks)
    finally:
        conn.close()

    return {
        "source_filename": file.filename,
        "source_type": source_type,
        "category": resolved_category,
        "chunks": [{"id": cid, **chunk} for cid, chunk in zip(chunk_ids, chunks)],
    }


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
