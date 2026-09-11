"""
SQLite storage for uploaded documents, with full-text search via FTS5.

Deliberately not a bigger database - the whole point of this layer is
"store markdown, search it by keyword" for a personal knowledge base, and
SQLite+FTS5 does that with zero infrastructure to run or operate.

One row is one CHUNK (roughly one page of source material), not one whole
uploaded file - a multi-page book becomes many rows sharing a
source_filename, so search/compile-a-book can point at the specific page
that matched instead of "somewhere in this 200-page file".

Schema note: this replaced an earlier one-row-per-file schema (filename +
markdown only). There's no migration for that - delete data/ajandb.sqlite3
and re-upload if you have an old database file; nothing here reads the old
column layout.
"""
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ajandb.sqlite3"
)


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_filename TEXT NOT NULL,
            source_type TEXT NOT NULL,
            page_number INTEGER NOT NULL DEFAULT 1,
            total_pages INTEGER NOT NULL DEFAULT 1,
            uploaded_at TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Uncategorized',
            markdown TEXT NOT NULL,
            confidence REAL,
            needs_review INTEGER NOT NULL DEFAULT 0
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            source_filename, markdown, category, content='documents', content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, source_filename, markdown, category)
            VALUES (new.id, new.source_filename, new.markdown, new.category);
        END;

        CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, source_filename, markdown, category)
            VALUES ('delete', old.id, old.source_filename, old.markdown, old.category);
        END;

        CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, source_filename, markdown, category)
            VALUES ('delete', old.id, old.source_filename, old.markdown, old.category);
            INSERT INTO documents_fts(rowid, source_filename, markdown, category)
            VALUES (new.id, new.source_filename, new.markdown, new.category);
        END;
        """
    )
    conn.commit()


def insert_chunks(conn: sqlite3.Connection, source_filename: str, source_type: str,
                   category: str, chunks: list[dict]) -> list[int]:
    """`chunks`: [{"page_number", "markdown", "confidence", "needs_review"}, ...]
    (see backend/convert.py). All chunks from one upload share source_filename,
    source_type, category and uploaded_at; total_pages is len(chunks)."""
    uploaded_at = datetime.now(timezone.utc).isoformat()
    total_pages = len(chunks)
    ids = []
    for chunk in chunks:
        cur = conn.execute(
            """
            INSERT INTO documents
                (source_filename, source_type, page_number, total_pages,
                 uploaded_at, category, markdown, confidence, needs_review)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_filename, source_type, chunk["page_number"], total_pages,
             uploaded_at, category, chunk["markdown"], chunk.get("confidence"),
             int(bool(chunk.get("needs_review")))),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def get_document(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, source_filename, source_type, page_number, total_pages,
               uploaded_at, category, confidence, needs_review
        FROM documents ORDER BY id DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _sanitize_fts_query(query: str) -> str:
    """Quotes each token so punctuation/special FTS5 syntax characters in
    user input (", *, -, etc.) can't break the MATCH query, then ORs them
    together - broad recall is the right default for "does any file mention
    this topic" search, and for feeding the book compiler upstream."""
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict]:
    if not query.strip():
        return []
    fts_query = _sanitize_fts_query(query)
    rows = conn.execute(
        """
        SELECT documents.id, documents.source_filename, documents.source_type,
               documents.page_number, documents.total_pages, documents.uploaded_at,
               documents.category, documents.confidence, documents.needs_review,
               snippet(documents_fts, 1, '<mark>', '</mark>', '…', 12) AS snippet
        FROM documents_fts
        JOIN documents ON documents.id = documents_fts.rowid
        WHERE documents_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query, limit),
    ).fetchall()
    return [dict(r) for r in rows]
