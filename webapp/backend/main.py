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


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    engine: str = Form("classical"),
    provider: str | None = Form(None),
    model: str | None = Form(None),
    category: str | None = Form(None),
):
    """`engine`="vision" routes scanned PDF pages through a per-page LLM call
    (`provider`: "anthropic" or "openai") instead of local Tesseract - see
    convert.pdf_to_markdown_vision. Needs the matching API key set in the
    backend's environment; that's separate from a claude.ai/ChatGPT Plus
    subscription.

    `category` is optional - leave it blank to have it auto-suggested from
    the converted content (falls back to "Uncategorized" if no API key is
    available for the suggestion call; that's never a hard failure).

    The file is chunked to roughly one page per row (see convert.py), each
    carrying its own OCR confidence and a needs_review flag."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in convert.SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext or '(none)'}")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        chunks = convert.convert_to_markdown(
            tmp_path, file.filename, engine=engine, provider=provider, model=model)
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


@app.post("/api/chat")
def chat(req: ChatRequest):
    conn = db.get_connection()
    try:
        return book_compiler.compile_book(req.instruction, conn, provider=req.provider, model=req.model)
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
