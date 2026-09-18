"""
Page-by-page review flow for PDF uploads: convert one page at a time, show
a human the source scan next to the converted markdown, and only save a
page to the database once they approve it (optionally after editing the
text) - so a bad page or an interrupted session only costs the pages not
yet approved, not the whole upload.

This is synchronous (no background thread/job-id polling like /api/upload
in main.py) - each request converts exactly one page, which is far more
likely to finish comfortably inside normal HTTP/tunnel timeouts than
converting a whole book in one request ever was.

Session state lives in memory only (same tradeoff as the async upload jobs
in main.py: a server restart mid-review loses that session - re-start the
review from scratch). Sessions are short-lived by design: a human is
actively looking at one while it's open.

State machine per session:
  - `pending` holds the page currently shown to the human, or None right
    after it's been resolved (approved/skipped) but the *next* page hasn't
    converted yet.
  - approve()/skip() require `pending` to be set - they resolve the current
    page, then try to convert the next one.
  - If that next-page conversion raises (e.g. a transient vision-LLM API
    error), `current_index` is left unchanged and `pending` stays None -
    nothing was silently duplicated or skipped. Call retry() to re-attempt
    exactly that page; retry() itself requires `pending` to be None (i.e.
    that something is actually stuck) so it can't be used to skip a page
    that's just waiting for approval.
"""
import base64
import os
import threading
import uuid

from . import categorize, convert, db

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def _log(session: dict, message: str):
    """Prints to the server console and appends to the session's own log -
    every review.py response includes that log so the frontend can render a
    running "converting / ready for review / saved" console instead of just
    a single current-status line."""
    print(f"[review {session['session_id'][:8]}] {message}", flush=True)
    session["log"].append(message)


def _get_session(session_id: str) -> dict:
    with _lock:
        session = _sessions.get(session_id)
    if not session:
        raise KeyError(session_id)
    return session


def _cleanup(session_id: str, session: dict):
    if os.path.exists(session["pdf_path"]):
        os.unlink(session["pdf_path"])
    with _lock:
        _sessions.pop(session_id, None)


def _convert_one_page(session: dict, page_index: int) -> dict:
    """Extracts, converts, and renders a preview image for exactly one page
    of the session's source PDF."""
    tmp_pdf = convert.extract_single_page_pdf(session["pdf_path"], page_index)
    try:
        png_bytes = convert.render_page_preview(tmp_pdf, 0)
        if session["engine"] == "vision":
            chunks = convert.pdf_to_markdown_vision(
                tmp_pdf, provider=session["provider"], model=session["model"],
                api_key=session["api_key"])
        else:
            chunks = convert.pdf_to_markdown(tmp_pdf, langs=session["langs"])
    finally:
        os.unlink(tmp_pdf)

    chunk = chunks[0]  # exactly one page in -> exactly one chunk out
    return {
        "page_number": page_index + 1,
        "markdown": chunk["markdown"],
        "confidence": chunk["confidence"],
        "needs_review": chunk["needs_review"],
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
    }


def start(pdf_path: str, filename: str, engine: str = "classical", provider: str = None,
          model: str = None, langs: str = None, category: str = None,
          api_key: str = None) -> dict:
    """Begins a review session for `pdf_path` (already saved to a temp file
    by the caller - this module takes ownership of it and deletes it when
    the session ends, is cancelled, or this call fails). Converts and
    returns page 1 immediately."""
    total_pages = convert.get_pdf_page_count(pdf_path)
    if total_pages < 1:
        raise ValueError(f"{filename} has no pages")

    session_id = uuid.uuid4().hex
    session = {
        "session_id": session_id,
        "pdf_path": pdf_path,
        "filename": filename,
        "engine": engine,
        "provider": provider,
        "model": model,
        "langs": langs,
        "api_key": api_key,
        "category": (category or "").strip() or None,
        "total_pages": total_pages,
        "current_index": None,
        "pending": None,
        "saved_chunk_ids": [],
        "log": [],
    }
    _log(session, f"Started: {filename} ({total_pages} page(s), engine={engine})")
    _log(session, f"Converting page 1/{total_pages}...")

    try:
        page = _convert_one_page(session, 0)  # may raise - session is not registered below if so
    except Exception as e:
        _log(session, f"ERROR converting page 1/{total_pages}: {e}")
        raise
    session["current_index"] = 0
    session["pending"] = page
    _log(session, f"Page 1/{total_pages} ready for review")
    with _lock:
        _sessions[session_id] = session

    return {"session_id": session_id, "total_pages": total_pages, "page": page, "log": session["log"]}


def _advance(session_id: str, session: dict) -> dict:
    """Converts and stores the page after `current_index` as the new
    `pending`, or ends the session if there isn't one. Raising here leaves
    `current_index`/`pending` exactly as retry() expects to find them."""
    next_index = session["current_index"] + 1
    if next_index >= session["total_pages"]:
        _log(session, f"Finished: {len(session['saved_chunk_ids'])} page(s) saved")
        log = session["log"]
        _cleanup(session_id, session)
        return {
            "done": True, "page": None,
            "total_saved": len(session["saved_chunk_ids"]),
            "category": session["category"] or "Uncategorized",
            "log": log,
        }

    _log(session, f"Converting page {next_index + 1}/{session['total_pages']}...")
    try:
        page = _convert_one_page(session, next_index)
    except Exception as e:
        _log(session, f"ERROR converting page {next_index + 1}/{session['total_pages']}: {e}")
        raise
    session["current_index"] = next_index
    session["pending"] = page
    _log(session, f"Page {next_index + 1}/{session['total_pages']} ready for review")
    return {
        "done": False, "page": page,
        "total_saved": len(session["saved_chunk_ids"]),
        "category": session["category"],
        "log": session["log"],
    }


def approve(session_id: str, markdown: str = None, needs_review: bool = None) -> dict:
    """Saves the currently-pending page - with `markdown`/`needs_review`
    overridden if given, e.g. a human edit before saving - then converts
    and returns the next page, or ends the session if that was the last
    one."""
    session = _get_session(session_id)
    pending = session["pending"]
    if pending is None:
        raise RuntimeError(
            "No page is currently awaiting review for this session - if the "
            "last request failed, call retry() instead of approve() again.")

    chunk = {
        "page_number": pending["page_number"],
        "markdown": markdown if markdown is not None else pending["markdown"],
        "confidence": pending["confidence"],
        "needs_review": pending["needs_review"] if needs_review is None else needs_review,
    }

    if session["category"] is None:
        session["category"] = categorize.suggest_category(
            chunk["markdown"], provider=session["provider"], model=session["model"],
            api_key=session["api_key"]) or "Uncategorized"

    conn = db.get_connection()
    try:
        ids = db.insert_chunks(
            conn, session["filename"], "pdf", session["category"], [chunk],
            total_pages=session["total_pages"])
    finally:
        conn.close()
    session["saved_chunk_ids"].extend(ids)
    session["pending"] = None
    _log(session, f"Saved page {chunk['page_number']}/{session['total_pages']} (chunk id {ids[0]})")

    return _advance(session_id, session)


def skip(session_id: str) -> dict:
    """Moves on without saving the currently-pending page."""
    session = _get_session(session_id)
    if session["pending"] is None:
        raise RuntimeError(
            "No page is currently awaiting review for this session - if the "
            "last request failed, call retry() instead of skip() again.")

    _log(session, f"Skipped page {session['pending']['page_number']}/"
                  f"{session['total_pages']} (not saved)")
    session["pending"] = None
    return _advance(session_id, session)


def retry(session_id: str) -> dict:
    """Re-attempts converting the page after the last approve()/skip() call,
    for when that conversion itself raised (e.g. a transient vision-LLM API
    error). Only valid when nothing is currently pending - i.e. something
    is actually stuck - so it can't be used to skip a page that's just
    waiting for a decision."""
    session = _get_session(session_id)
    if session["pending"] is not None:
        raise RuntimeError("A page is already awaiting review - approve or skip it first.")
    return _advance(session_id, session)


def cancel(session_id: str) -> dict:
    """Ends the session without converting or saving anything further.
    Pages already approved before cancelling stay in the database."""
    session = _get_session(session_id)
    _log(session, f"Cancelled after page {session['current_index'] + 1}/"
                  f"{session['total_pages']} ({len(session['saved_chunk_ids'])} page(s) saved)")
    log = session["log"]
    _cleanup(session_id, session)
    return {
        "total_saved": len(session["saved_chunk_ids"]),
        "category": session["category"] or "Uncategorized",
        "log": log,
    }
