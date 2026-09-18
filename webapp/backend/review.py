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
import difflib
import os
import re
import threading
import uuid

from . import categorize, convert, db

_sessions: dict[str, dict] = {}
_lock = threading.Lock()

_WORD_TOKEN_RE = re.compile(r"\S+|\s+")


def _word_diff(a: str, b: str) -> dict:
    """Word-level diff between two transcriptions of the same page - lets a
    human see exactly which words two models disagree on, instead of
    trusting either one's own (unreliable, self-reported for a vision-LLM)
    confidence score alone. Tokenizes on whitespace boundaries rather than
    characters, so a single retyped word shows as one change, not a
    scattering of single-character ones.

    Returns {"segments": [...], "agreement_ratio": 0-100}. Each segment is
    {"tag": "equal", "text": ...} where both versions agree, or
    {"tag": "replace"|"delete"|"insert", "a": ..., "b": ...} where they
    differ ("a" is the first text, "b" the second)."""
    a_tokens = _WORD_TOKEN_RE.findall(a)
    b_tokens = _WORD_TOKEN_RE.findall(b)
    matcher = difflib.SequenceMatcher(None, a_tokens, b_tokens, autojunk=False)
    segments = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segments.append({"tag": "equal", "text": "".join(a_tokens[i1:i2])})
        else:
            segments.append({"tag": tag, "a": "".join(a_tokens[i1:i2]), "b": "".join(b_tokens[j1:j2])})
    return {"segments": segments, "agreement_ratio": round(matcher.ratio() * 100, 1)}


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
        # exact substrings of `markdown` that dragged confidence below 100 -
        # the frontend highlights them so a human reviewing the page knows
        # exactly what to check first instead of re-reading the whole thing.
        "flagged_snippets": chunk.get("flagged_snippets", []),
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


def verify_second_opinion(session_id: str, provider: str = None, model: str = None,
                           api_key: str = None) -> dict:
    """Re-converts the currently-pending page with the vision-LLM engine
    under `provider`/`model`/`api_key` - independent of whatever engine or
    provider actually produced the pending transcription - and returns
    both texts plus a word-level diff (see _word_diff). A cross-check
    against a second model, since neither engine's own confidence score is
    fully trustworthy on its own (Tesseract's is a real per-character
    score but still just one engine's self-measurement; a vision-LLM's is
    literally the model guessing how sure it is, which is well known to
    correlate poorly with actual accuracy) - agreement between two
    independently-run models is a much stronger signal, and the diff shows
    exactly which words to look at if they don't. Doesn't touch the
    session's approve/skip state at all - purely a side-check on the page
    currently being reviewed, safe to call as many times as you like."""
    session = _get_session(session_id)
    pending = session["pending"]
    if pending is None:
        raise RuntimeError(
            "No page is currently awaiting review for this session - approve, "
            "skip, or retry first.")

    _log(session, f"Verifying page {pending['page_number']}/{session['total_pages']} "
                  f"against {provider or 'anthropic'}...")
    tmp_pdf = convert.extract_single_page_pdf(session["pdf_path"], session["current_index"])
    try:
        chunks = convert.pdf_to_markdown_vision(tmp_pdf, provider=provider, model=model, api_key=api_key)
    except Exception as e:
        _log(session, f"ERROR verifying page {pending['page_number']}: {e}")
        raise
    finally:
        os.unlink(tmp_pdf)

    second_markdown = chunks[0]["markdown"]
    diff = _word_diff(pending["markdown"], second_markdown)
    _log(session, f"Verification agreement: {diff['agreement_ratio']}%")
    return {
        "provider": provider or "anthropic",
        "original_markdown": pending["markdown"],
        "second_markdown": second_markdown,
        "diff": diff["segments"],
        "agreement_ratio": diff["agreement_ratio"],
        "log": session["log"],
    }


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
