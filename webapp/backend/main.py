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

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import book_compiler, convert, db

app = FastAPI(title="AjanDB")

_FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in convert.SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext or '(none)'}")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        markdown = convert.convert_to_markdown(tmp_path, file.filename)
    except Exception as e:
        raise HTTPException(422, f"Failed to convert {file.filename}: {e}")
    finally:
        os.unlink(tmp_path)

    conn = db.get_connection()
    try:
        doc_id = db.insert_document(conn, file.filename, markdown)
    finally:
        conn.close()

    return {"id": doc_id, "filename": file.filename, "markdown": markdown}


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


@app.post("/api/chat")
def chat(req: ChatRequest):
    conn = db.get_connection()
    try:
        return book_compiler.compile_book(req.instruction, conn)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Book compilation failed: {e}")
    finally:
        conn.close()


# Serve the frontend last so it doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
