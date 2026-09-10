"""
SQLite storage for uploaded documents, with full-text search via FTS5.

Deliberately not a bigger database - the whole point of this layer is
"store markdown, search it by keyword" for a personal knowledge base, and
SQLite+FTS5 does that with zero infrastructure to run or operate.
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
            filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            markdown TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            filename, markdown, content='documents', content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, filename, markdown)
            VALUES (new.id, new.filename, new.markdown);
        END;

        CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, filename, markdown)
            VALUES ('delete', old.id, old.filename, old.markdown);
        END;

        CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, filename, markdown)
            VALUES ('delete', old.id, old.filename, old.markdown);
            INSERT INTO documents_fts(rowid, filename, markdown)
            VALUES (new.id, new.filename, new.markdown);
        END;
        """
    )
    conn.commit()


def insert_document(conn: sqlite3.Connection, filename: str, markdown: str) -> int:
    cur = conn.execute(
        "INSERT INTO documents (filename, uploaded_at, markdown) VALUES (?, ?, ?)",
        (filename, datetime.now(timezone.utc).isoformat(), markdown),
    )
    conn.commit()
    return cur.lastrowid


def get_document(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, filename, uploaded_at FROM documents ORDER BY id DESC"
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
        SELECT documents.id, documents.filename, documents.uploaded_at,
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
