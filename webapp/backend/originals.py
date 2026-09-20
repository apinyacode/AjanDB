"""
Permanent storage of the original file a book was uploaded from, so the
Data Search tab can offer "View original" / "Export original" buttons
alongside the converted markdown.

This is deliberately separate from sources.py, even though the two look
similar. sources.py keeps a copy only while a page-by-page review session
is *incomplete*, and deletes it the moment that session finishes - it
exists purely to make resuming possible. This module keeps a copy for
every book, for as long as the book itself exists, regardless of whether
it came from a review session or a one-shot bulk upload, and regardless
of file type (pdf/docx/txt/md) - it exists so a user can always get back
the exact file they gave this app. Conflating the two would mean either
resuming loses its "only while incomplete" cleanup, or every book's
original would get deleted the moment review finishes - neither is right,
so they're kept as independent stores with independent lifecycles.

Keyed by (source_filename, total_pages) - the same "book identity" used
throughout backend/db.py and sources.py.

`scripts/chunked_upload.py` never calls this: it works directly on a file
path the user already has on disk, outside the webapp entirely, so there's
no "original upload" for this app to keep a copy of.
"""
import hashlib
import os
import shutil

ORIGINALS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "originals"
)


def _book_key(source_filename: str, total_pages: int) -> str:
    digest = hashlib.sha256(f"{source_filename}::{total_pages}".encode("utf-8")).hexdigest()
    return digest[:24]


def _path(source_filename: str, total_pages: int) -> str:
    key = _book_key(source_filename, total_pages)
    ext = os.path.splitext(source_filename)[1]
    return os.path.join(ORIGINALS_DIR, f"{key}{ext}")


def save(file_path: str, source_filename: str, total_pages: int) -> None:
    """Copies `file_path` into permanent storage under this book's key, if
    it isn't already there. A no-op when it is, since a multi-batch upload
    (or a review session plus a later re-upload of the same book) can call
    this more than once for the same (source_filename, total_pages)."""
    dest = _path(source_filename, total_pages)
    if os.path.exists(dest):
        return
    os.makedirs(ORIGINALS_DIR, exist_ok=True)
    shutil.copy(file_path, dest)


def load(source_filename: str, total_pages: int) -> str | None:
    """Returns the path to this book's stored original, or None if it was
    never saved (e.g. it predates this feature, or it was ingested via
    scripts/chunked_upload.py)."""
    path = _path(source_filename, total_pages)
    return path if os.path.exists(path) else None


def exists(source_filename: str, total_pages: int) -> bool:
    return os.path.exists(_path(source_filename, total_pages))


def delete(source_filename: str, total_pages: int) -> None:
    """Called when the book itself is deleted (see main.py's delete_book
    endpoint) - there's nothing left to view or export at that point."""
    path = _path(source_filename, total_pages)
    if os.path.exists(path):
        os.unlink(path)
