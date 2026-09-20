"""
Durable storage for the *original* uploaded PDF of a page-by-page review
session, so an incomplete book (cancelled partway through review, or just
abandoned - browser closed, server restarted) can be resumed later instead
of starting over from page 1 - see review.py's start()/resume()/cancel().

This is new: every other part of this app deliberately discards the
source file once conversion finishes (the whole design keeps only the
resulting markdown/images, not the original upload - see the README's
"Where is the output .md saved?" note). A review session now keeps a copy
specifically because it can end - cancelled or simply abandoned - long
before conversion is done, and the only way to continue from where it
left off is to still have the file. The API key used for that session is
never part of what's saved here (same "never persisted server-side" rule
every other browser-supplied key in this app follows) - resuming a
vision-engine session that has no key in the backend's own environment
will need one supplied again at that point.

Bulk /api/upload never goes through this: it converts a whole file in one
shot and only saves anything to the database if that whole conversion
succeeds, so there's never a partial result to resume - either the upload
worked, or nothing was saved and re-uploading is the only option anyway.

Keyed by (source_filename, total_pages) - the same "book identity" used
throughout backend/db.py - since a review session doesn't have a database
row of its own until at least one page is approved.
"""
import hashlib
import json
import os
import shutil

SOURCES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sources"
)


def _book_key(source_filename: str, total_pages: int) -> str:
    digest = hashlib.sha256(f"{source_filename}::{total_pages}".encode("utf-8")).hexdigest()
    return digest[:24]


def _paths(source_filename: str, total_pages: int) -> tuple[str, str]:
    key = _book_key(source_filename, total_pages)
    return (
        os.path.join(SOURCES_DIR, f"{key}.pdf"),
        os.path.join(SOURCES_DIR, f"{key}.json"),
    )


def save(pdf_path: str, source_filename: str, total_pages: int, settings: dict) -> None:
    """Copies `pdf_path` into durable storage and records `settings`
    (engine/provider/model/langs/category/auto_validate/next_index) as its
    resume point - called once from review.start(). `pdf_path` itself is
    untouched (review.py owns and eventually deletes its own copy)."""
    os.makedirs(SOURCES_DIR, exist_ok=True)
    pdf_dest, json_dest = _paths(source_filename, total_pages)
    shutil.copy(pdf_path, pdf_dest)
    with open(json_dest, "w", encoding="utf-8") as f:
        json.dump(settings, f)


def update(source_filename: str, total_pages: int, settings: dict) -> None:
    """Overwrites the settings recorded for this book - called after every
    approve()/skip() (see review._advance()) with the session's *current*
    full settings (not just next_index), so a category that only gets
    decided partway through (auto-suggested on the first approval, not
    known yet at start()) is still there if this session is cancelled or
    abandoned and later resumed - a partial update that touched only
    next_index would leave a resumed session re-guessing a category that
    might not match the pages already saved under it. A no-op if nothing
    was ever saved for this book (e.g. it was already cleaned up by
    _advance()'s "done" branch)."""
    _, json_path = _paths(source_filename, total_pages)
    if not os.path.exists(json_path):
        return
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(settings, f)


def load(source_filename: str, total_pages: int) -> tuple[str, dict] | None:
    """Returns (pdf_path, settings) if this book has a resumable source
    saved, or None. `pdf_path` points at the durable copy - callers must
    make their own throwaway copy before handing it to anything that takes
    ownership of (and deletes) its input, like review.py's session
    machinery."""
    pdf_path, json_path = _paths(source_filename, total_pages)
    if not (os.path.exists(pdf_path) and os.path.exists(json_path)):
        return None
    with open(json_path, "r", encoding="utf-8") as f:
        settings = json.load(f)
    return pdf_path, settings


def exists(source_filename: str, total_pages: int) -> bool:
    pdf_path, json_path = _paths(source_filename, total_pages)
    return os.path.exists(pdf_path) and os.path.exists(json_path)


def delete(source_filename: str, total_pages: int) -> None:
    """Called once a review session reaches its last page naturally (see
    review._advance()'s "done" branch) - every page has been approved or
    deliberately skipped by then, so there's nothing left to resume and no
    reason to keep the original file around. Never called from cancel():
    that's exactly the case resuming exists for."""
    pdf_path, json_path = _paths(source_filename, total_pages)
    for path in (pdf_path, json_path):
        if os.path.exists(path):
            os.unlink(path)
