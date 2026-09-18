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
                   category: str, chunks: list[dict], total_pages: int | None = None) -> list[int]:
    """`chunks`: [{"page_number", "markdown", "confidence", "needs_review"}, ...]
    (see backend/convert.py). All chunks from one upload share source_filename,
    source_type, category and uploaded_at.

    `total_pages` defaults to len(chunks) (the normal single-upload case).
    Pass it explicitly when inserting a source file's chunks in more than one
    batch - e.g. scripts/chunked_upload.py processing a large book a few
    pages at a time - so every batch reports the true total instead of just
    its own slice's size."""
    uploaded_at = datetime.now(timezone.utc).isoformat()
    if total_pages is None:
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


_LATEST_PER_PAGE_CTE = """
    WITH latest_per_page AS (
        SELECT d.* FROM documents d
        WHERE d.id = (
            SELECT d2.id FROM documents d2
            WHERE d2.source_filename = d.source_filename
              AND d2.total_pages = d.total_pages
              AND d2.page_number = d.page_number
            ORDER BY d2.uploaded_at DESC, d2.id DESC
            LIMIT 1
        )
    )
"""
# Re-running an upload for the same page (e.g. scripts/chunked_upload.py
# without --resume, or just re-uploading the same file) inserts a second row
# for that page rather than overwriting the first - documented elsewhere as
# an accepted limitation of the raw chunk store. Browsing/exporting a "book"
# should still show its *current* pages once each, though, not double-count
# or repeat a page that happens to exist twice - so both queries below
# collapse to the most-recently-uploaded row per (source_filename,
# total_pages, page_number) before doing anything else with the rows.


def list_books(conn: sqlite3.Connection) -> list[dict]:
    """Groups chunks into "books" for browsing - one entry per uploaded
    source file, whether it needed conversion (a scanned PDF) or was stored
    as-is (.txt/.md/.docx). The schema has no real book/upload id, and
    can't: chunked_upload.py and the page-by-page review flow each insert
    one source file's chunks across many separate insert_chunks() calls (one
    per batch or even per page), so every chunk gets its own `uploaded_at` -
    grouping by that would show a 28-page reviewed book as 28 "books".
    (source_filename, total_pages) is stable across all of those calls for
    one logical upload instead, since every insert for the same file is
    told the file's true total page count - so it's used as the group key,
    with the group's lowest row id serving as a lightweight "book id" for
    get_book_chunks()/export. Two different files that happen to share both
    a filename and a page count would incorrectly merge into one entry; a
    real book id column would be the fix if that ever matters in practice."""
    rows = conn.execute(
        _LATEST_PER_PAGE_CTE
        + """
        SELECT MIN(id) AS id, source_filename, source_type, total_pages,
               MAX(category) AS category,
               COUNT(*) AS pages_stored,
               SUM(needs_review) AS needs_review_count,
               AVG(confidence) AS avg_confidence,
               MIN(uploaded_at) AS first_uploaded_at,
               MAX(uploaded_at) AS last_uploaded_at
        FROM latest_per_page
        GROUP BY source_filename, total_pages
        ORDER BY last_uploaded_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_book_chunks(conn: sqlite3.Connection, book_id: int) -> list[dict] | None:
    """`book_id` is the `id` field from list_books() (that group's lowest
    row id, not a real book id column - see list_books()'s docstring).
    Returns the current chunk for each page sharing that row's
    (source_filename, total_pages), ordered by page number, or None if
    book_id doesn't match any row."""
    anchor = conn.execute(
        "SELECT source_filename, total_pages FROM documents WHERE id = ?", (book_id,)
    ).fetchone()
    if not anchor:
        return None
    rows = conn.execute(
        _LATEST_PER_PAGE_CTE
        + """
        SELECT * FROM latest_per_page
        WHERE source_filename = ? AND total_pages = ?
        ORDER BY page_number
        """,
        (anchor["source_filename"], anchor["total_pages"]),
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
