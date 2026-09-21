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
in main.py: a server restart mid-review loses that session). Sessions are
short-lived by design: a human is actively looking at one while it's open.
Losing the in-memory session doesn't lose the *pages already approved*
(they're in the database) or the ability to pick up the rest, though - see
resume() and sources.py: every session's original PDF plus its settings
and last-reached page are kept in durable storage until the session
finishes every page (cancelling or simply abandoning it - closing the
browser, a server restart - leaves that storage in place specifically so
resume() has something to work from).

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
import concurrent.futures
import os
import shutil
import tempfile
import threading
import time
import uuid

from . import categorize, convert, db, ensemble_verify, originals, sources, spellcheck

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


def _dedup_flags(snippets: list[str]) -> list[str]:
    """Removes exact duplicate strings, preserving first-seen order - once
    confidence flags, cross-provider disagreement, Tesseract disagreement,
    and logprob flags are all unioned into one list (see _convert_one_page),
    the same span can easily get flagged by more than one source. The
    frontend's highlight renderer already handles genuinely overlapping (not
    identical) spans on its own, by preferring the longest match at a given
    position - this just keeps the list itself free of redundant repeats."""
    seen = set()
    result = []
    for s in snippets:
        if s and s not in seen:
            seen.add(s)
            result.append(s)
    return result


def _diff_flags(a: str, b: str) -> list[str]:
    """Runs ensemble_verify.word_diff(a, b) and returns just the
    disagreeing spans from `a`'s side, stripped - the shared core of every
    cross-check below."""
    diff = ensemble_verify.word_diff(a, b)
    return [seg["a"].strip() for seg in diff["segments"]
            if seg["tag"] != "equal" and seg.get("a", "").strip()]


def _cross_provider_disagreement_flags(session: dict, page_index: int, chunk: dict) -> list[str]:
    """Automatic version of verify_second_opinion (see that function's own
    docstring for why a second, independently-run model is a stronger
    signal than either engine's own confidence score) - runs for any page
    whose own confidence is below 100%, regardless of which engine produced
    it, instead of only when a reviewer opts in. Always resolves its API
    key from the backend's own environment, never a browser-supplied one:
    there's no mechanism for a reviewer to hand this session a *second*
    provider's key mid-review (the session already has one key, for its own
    engine/provider). Silently skipped - never blocks or fails page
    conversion - if that environment key isn't configured; a human can
    still run "Verify with second model" manually with a browser-supplied
    key from the review UI. This is the one check here that costs a real
    API call automatically - see CONTEXT.md's Known limitations."""
    cross_provider = "openai" if (session["provider"] or "anthropic") == "anthropic" else "anthropic"
    env_var = "OPENAI_API_KEY" if cross_provider == "openai" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(env_var)
    if not api_key:
        return []

    tmp_pdf = convert.extract_single_page_pdf(session["pdf_path"], page_index)
    try:
        second_chunks = convert.pdf_to_markdown_vision(tmp_pdf, provider=cross_provider, api_key=api_key)
    except Exception as e:
        _log(session, f"Automatic cross-check against {cross_provider} skipped: {e}")
        return []
    finally:
        os.unlink(tmp_pdf)

    flags = _diff_flags(chunk["markdown"], second_chunks[0]["markdown"])
    if flags:
        _log(session, f"Automatic cross-check against {cross_provider} flagged "
                       f"{len(flags)} disagreement span(s)")
    return flags


def _tesseract_cross_check_flags(png_bytes: bytes, vision_markdown: str, langs: str) -> list[str]:
    """Free, local second opinion for vision-engine pages: runs classical
    Tesseract OCR (the same engine the classical engine itself uses) on the
    identical rendered page image already used for the vision-LLM pass, and
    flags spans where the two disagree. Unlike
    _cross_provider_disagreement_flags above, this costs nothing and needs
    no API key, so it always runs for a vision-engine page below 100%
    confidence."""
    from pipeline import ocr as pdf_ocr

    result = pdf_ocr.process_scanned_page(png_bytes, langs=langs or pdf_ocr.DEFAULT_LANGS)
    tesseract_text = "\n".join(line["text"] for line in result["lines"]).strip()
    if not tesseract_text:
        return []
    return _diff_flags(vision_markdown, tesseract_text)


# Below this average per-token log-probability (natural log, so -1.5 ~= a
# 22% token probability), a GPT-4o transcribed span is flagged as worth a
# second look. Chosen as a reasonable "clearly hesitant" cutoff, not derived
# from calibration data - like the other checks here, a hint, not a verdict.
_LOGPROB_FLAG_THRESHOLD = -1.5


def _avg_logprob_for_span(raw_text: str, token_logprobs: list[tuple[str, float]],
                           span_text: str) -> float | None:
    """Finds `span_text` as a literal substring of `raw_text` (reconstructed
    by concatenating token strings in completion order) and averages the
    logprobs of every token whose character range overlaps that span.
    Returns None if the span can't be located (e.g. the model reformatted
    whitespace) - callers skip flagging rather than guessing."""
    start = raw_text.find(span_text)
    if start == -1 or not span_text:
        return None
    end = start + len(span_text)

    offset = 0
    logprobs_in_span = []
    for token, logprob in token_logprobs:
        tok_start, tok_end = offset, offset + len(token)
        if tok_end > start and tok_start < end:
            logprobs_in_span.append(logprob)
        offset = tok_end
    if not logprobs_in_span:
        return None
    return sum(logprobs_in_span) / len(logprobs_in_span)


def _openai_logprob_flags(png_bytes: bytes, model: str, api_key: str) -> list[str]:
    """GPT-4o only (see extract_page_with_vision_logprobs - the Anthropic
    API doesn't expose per-token logprobs): re-sends the page with logprobs
    requested, then flags any transcribed text block whose average
    per-token log-probability falls below _LOGPROB_FLAG_THRESHOLD. This is
    a second, independent request against the same provider/page (the
    original conversion didn't ask for logprobs), so - like
    _cross_provider_disagreement_flags - it costs an extra API call."""
    from pipeline import vision_ocr

    blocks, raw_text, token_logprobs = vision_ocr.extract_page_with_vision_logprobs(
        png_bytes, model=model, api_key=api_key)
    if not token_logprobs:
        return []

    flags = []
    for block in blocks:
        if block.get("type") != "text" or not block.get("text"):
            continue
        avg_logprob = _avg_logprob_for_span(raw_text, token_logprobs, block["text"])
        if avg_logprob is not None and avg_logprob < _LOGPROB_FLAG_THRESHOLD:
            flags.append(convert._clean(block["text"]).strip())
    return flags


# A page's own primary conversion must always be allowed to take however
# long a model call takes - that's the transcription the reviewer is there
# to see, not optional. The cross-checks below are different: each one is
# an extra API call *on top of* that, stacking on the very pages where the
# primary call was already slowest (vision-engine pages) - run
# sequentially, two or three of them could easily push a single review
# request past a tunnel/proxy's own (often much shorter) timeout, which
# surfaces to the reviewer as a broken "<!DOCTYPE ...> is not valid JSON"
# error instead of a slow-but-working page. So: run every API-calling
# cross-check concurrently rather than one after another, and give up on
# any that hasn't finished within this shared budget - a cross-check that
# times out is simply skipped (logged, not an error), never a reason to
# fail or delay the page itself.
_CROSS_CHECK_TIMEOUT_SECONDS = 20


def _run_cross_checks_with_timeout(jobs: list) -> list:
    """Runs every zero-arg callable in `jobs` in its own thread, all started
    at once, and collects results within a single shared
    _CROSS_CHECK_TIMEOUT_SECONDS budget total (not per job) - so queuing up
    more cross-checks never multiplies how long a reviewer waits. A job
    that raises or doesn't finish in time contributes `[]` rather than
    failing the whole page - generic over what each job actually returns
    (a list of flagged spans for the checks below, or a (provider,
    markdown) tuple for _run_ensemble_verification), since the caller
    already knows what shape to expect from its own jobs."""
    if not jobs:
        return []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs))
    futures = [pool.submit(job) for job in jobs]
    deadline = time.monotonic() + _CROSS_CHECK_TIMEOUT_SECONDS
    results = []
    try:
        for future in futures:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                results.append(future.result(timeout=remaining))
            except Exception:
                results.append([])
    finally:
        # Don't block on stragglers - an abandoned thread's API call just
        # finishes in the background and its result is discarded; waiting
        # here would defeat the entire point of the timeout above.
        pool.shutdown(wait=False)
    return results


_ENSEMBLE_API_KEY_ENV_VAR = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

# Below this word-tokenization agreement between pythainlp's "newmm" and
# "longest" engines, a Thai disagreement span is additionally marked
# high_divergence - a second, independent signal (alongside the two
# providers disagreeing at all) that the span is genuinely garbled, not
# just a stylistic difference. Like _LOGPROB_FLAG_THRESHOLD above, a
# reasonable "clearly diverging" cutoff, not derived from calibration data.
_TOKENIZER_DIVERGENCE_THRESHOLD = 70.0


def _annotate_thai_divergence(spans: list[dict]) -> None:
    """Mutates `spans` (see ensemble_verify.diff_ensemble) in place, adding
    high_divergence=True to any Thai disagreement span whose two pythainlp
    tokenizations diverge sharply (see spellcheck.tokenizer_divergence) -
    the frontend renders these with a distinct, more attention-grabbing
    style than an ordinary ensemble disagreement (see style.css)."""
    for span in spans:
        if span["agreement"] != "none":
            continue
        text = next(iter(span["providers"].values()), "")
        if spellcheck.contains_thai(text) and spellcheck.tokenizer_divergence(text) < _TOKENIZER_DIVERGENCE_THRESHOLD:
            span["high_divergence"] = True


def _run_ensemble_verification(session: dict, page_index: int, chunk: dict) -> dict | None:
    """Transcribes this page independently with both ensemble providers
    (see ensemble_verify.py) and diffs them against each other - unlike
    _cross_provider_disagreement_flags above (a single extra call against
    whichever provider *wasn't* already used, diffed against the primary
    conversion), this is a full two-model ensemble: when the session's own
    primary engine is already a vision provider in the ensemble, that
    provider's already-converted markdown is reused as one of the two
    ensemble members (avoiding a redundant duplicate call to it), and only
    the *other* provider is called fresh; for a classical-engine session,
    neither ensemble provider has been called at all yet, so both are
    called fresh - hence the frontend's "2x cost/latency" framing, which is
    exactly true for a vision-engine session and a full two fresh calls for
    a classical one.

    Resolves API keys from the backend's own environment only (same "no
    mechanism for a reviewer to hand this session a second provider's key
    mid-review" rule _cross_provider_disagreement_flags already follows) -
    returns None (logged, not an error) if a needed key isn't configured,
    or if a call fails/times out, never a reason to fail or delay the page
    itself. Uses the same bounded-concurrency timeout as the other
    API-calling cross-checks (see _CROSS_CHECK_TIMEOUT_SECONDS). Each
    concurrent call re-extracts its own single-page copy of the PDF (same
    reasoning as _cross_provider_disagreement_flags above: `_convert_one_page`'s
    own tmp_pdf is already deleted by the time cross-checks run, and two
    threads each doing their own extract+cleanup is simpler than sharing
    one file across them)."""
    outputs = {}
    to_call = list(ensemble_verify.DEFAULT_ENSEMBLE_PROVIDERS)
    if session["engine"] == "vision" and session["provider"] in ensemble_verify.DEFAULT_ENSEMBLE_PROVIDERS:
        outputs[session["provider"]] = chunk["markdown"]
        to_call = [p for p in ensemble_verify.DEFAULT_ENSEMBLE_PROVIDERS if p != session["provider"]]

    missing_keys = [p for p in to_call if not os.environ.get(_ENSEMBLE_API_KEY_ENV_VAR[p])]
    if missing_keys:
        _log(session, f"Ensemble verification skipped - missing API key(s) for: {', '.join(missing_keys)}")
        return None

    def _call(provider):
        try:
            api_key = os.environ.get(_ENSEMBLE_API_KEY_ENV_VAR[provider])
            tmp_pdf = convert.extract_single_page_pdf(session["pdf_path"], page_index)
            try:
                result = ensemble_verify.convert_page_ensemble(tmp_pdf, providers=(provider,),
                                                                 api_keys={provider: api_key})
            finally:
                os.unlink(tmp_pdf)
            return provider, result[provider]
        except Exception as e:
            _log(session, f"Ensemble verification against {provider} skipped: {e}")
            return None

    jobs = [(lambda p=p: _call(p)) for p in to_call]
    for result in _run_cross_checks_with_timeout(jobs):
        if result:
            provider, markdown = result
            outputs[provider] = markdown

    if len(outputs) < 2:
        _log(session, "Ensemble verification skipped - a provider call failed, timed out, or was skipped")
        return None

    diff = ensemble_verify.diff_ensemble(outputs)
    _annotate_thai_divergence(diff["spans"])
    _log(session, f"Ensemble verification agreement: {diff['agreement_pct']}%")
    return diff


def _convert_one_page(session: dict, page_index: int) -> dict:
    """Extracts, converts, and renders a preview image for exactly one page
    of the session's source PDF. When the session opted into it
    (`auto_validate`, off by default - see start()), any page whose own
    confidence is below 100% additionally runs through the cross-check
    functions above - their disagreement spans are unioned into
    flagged_snippets alongside (not instead of) the original per-line
    confidence flags, since no single signal here is fully trustworthy on
    its own (see CONTEXT.md's Known limitations for the cost/coverage
    tradeoffs of each). The API-calling checks run concurrently, capped at
    _CROSS_CHECK_TIMEOUT_SECONDS total - see that constant's comment for
    why."""
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

    extra_flags = []
    needs_ocr_or_vision = chunk["confidence"] is not None and chunk["confidence"] < 100
    if session["auto_validate"] and needs_ocr_or_vision:
        jobs = [lambda: _cross_provider_disagreement_flags(session, page_index, chunk)]
        if session["engine"] == "vision":
            extra_flags += _tesseract_cross_check_flags(png_bytes, chunk["markdown"], session["langs"])
            if (session["provider"] or "anthropic") == "openai":
                jobs.append(lambda: _openai_logprob_flags(png_bytes, session["model"], session["api_key"]))
        for flags in _run_cross_checks_with_timeout(jobs):
            extra_flags += flags

    # Ensemble verification (own opt-in, separate from auto_validate above)
    # - see _run_ensemble_verification's docstring for why it's kept as its
    # own field/mechanism rather than folded into flagged_snippets: the two
    # ensemble outputs aren't always substrings of `chunk["markdown"]`
    # (a classical-engine page's ensemble diffs two fresh vision calls
    # against each other, neither of which is the Tesseract text shown),
    # so this isn't always highlightable the same way flagged_snippets is.
    ensemble_diff = None
    if session["ensemble_verify"] and needs_ocr_or_vision:
        ensemble_diff = _run_ensemble_verification(session, page_index, chunk)
    has_ensemble_disagreement = ensemble_diff is not None and any(
        s["agreement"] == "none" for s in ensemble_diff["spans"])

    flagged_snippets = _dedup_flags(chunk.get("flagged_snippets", []) + extra_flags)

    return {
        "page_number": page_index + 1,
        "markdown": chunk["markdown"],
        "confidence": chunk["confidence"],
        # a disagreement from any cross-check (or the ensemble check) above
        # is itself grounds for review, even on a page whose own confidence
        # score was high.
        "needs_review": chunk["needs_review"] or bool(extra_flags) or has_ensemble_disagreement,
        # exact substrings of `markdown` that dragged confidence below 100,
        # or that a cross-check above disagreed on - the frontend highlights
        # them so a human reviewing the page knows exactly what to check
        # first instead of re-reading the whole thing.
        "flagged_snippets": flagged_snippets,
        # Thai words not in pythainlp's dictionary, each with suggested
        # correction(s) - a separate, advisory-only highlight from the
        # confidence flags above (see spellcheck.py).
        "typos": spellcheck.find_thai_typos(chunk["markdown"]),
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
        # None unless ensemble_verify was on for this session AND this page
        # needed OCR/vision AND both provider calls actually succeeded -
        # see ensemble_verify.diff_ensemble for the span shape.
        "ensemble_diff": ensemble_diff["spans"] if ensemble_diff else None,
        "agreement_pct": ensemble_diff["agreement_pct"] if ensemble_diff else None,
    }


def start(pdf_path: str, filename: str, engine: str = "classical", provider: str = None,
          model: str = None, langs: str = None, category: str = None,
          api_key: str = None, auto_validate: bool = False, ensemble_verify: bool = False) -> dict:
    """Begins a review session for `pdf_path` (already saved to a temp file
    by the caller - this module takes ownership of it and deletes it when
    the session ends, is cancelled, or this call fails). Converts and
    returns page 1 immediately.

    `auto_validate` (off by default - an explicit opt-in, not the same
    "promoted to default" behavior an earlier version of this feature had)
    turns on the automatic multi-signal cross-checks in _convert_one_page
    for every page below 100% confidence in this session. Off by default
    because those checks cost extra API calls and - even bounded and run
    concurrently (see _run_cross_checks_with_timeout) - add real latency to
    every low-confidence page; a reviewer who wants that tradeoff opts in
    per upload via the frontend's "Second-model validation" checkbox.

    `ensemble_verify` (also off by default, independent of auto_validate -
    a reviewer can turn on either, both, or neither) turns on proactive
    ensemble verification (see ensemble_verify.py and this module's
    _run_ensemble_verification) for every page below 100% confidence in
    this session: both vision-LLM providers transcribe it independently,
    and their disagreement spans become the primary review signal for that
    page, instead of a human having to manually trigger "Verify with
    second model" one page at a time."""
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
        "auto_validate": bool(auto_validate),
        "ensemble_verify": bool(ensemble_verify),
        "category": (category or "").strip() or None,
        "total_pages": total_pages,
        "current_index": None,
        "pending": None,
        "saved_chunk_ids": [],
        "log": [],
    }
    _log(session, f"Started: {filename} ({total_pages} page(s), engine={engine})")
    if auto_validate:
        _log(session, "Second-model validation: on")
    if ensemble_verify:
        _log(session, "Ensemble verification: on")
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

    sources.save(pdf_path, filename, total_pages, _resume_settings(session, next_index=0))
    originals.save(pdf_path, filename, total_pages)

    return {"session_id": session_id, "total_pages": total_pages, "page": page, "log": session["log"]}


def _resume_settings(session: dict, next_index: int) -> dict:
    """The subset of session state resume() needs to continue later -
    never the API key (see sources.py's docstring on why)."""
    return {
        "engine": session["engine"],
        "provider": session["provider"],
        "model": session["model"],
        "langs": session["langs"],
        "category": session["category"],
        "auto_validate": session["auto_validate"],
        "ensemble_verify": session["ensemble_verify"],
        "next_index": next_index,
    }


def _advance(session_id: str, session: dict) -> dict:
    """Converts and stores the page after `current_index` as the new
    `pending`, or ends the session if there isn't one. Raising here leaves
    `current_index`/`pending` exactly as retry() expects to find them."""
    next_index = session["current_index"] + 1
    if next_index >= session["total_pages"]:
        _log(session, f"Finished: {len(session['saved_chunk_ids'])} page(s) saved")
        log = session["log"]
        # every page has now been approved or deliberately skipped - nothing
        # left to resume, unlike cancel() (see sources.py's docstring).
        sources.delete(session["filename"], session["total_pages"])
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
    sources.update(session["filename"], session["total_pages"], _resume_settings(session, next_index))
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
    diff = ensemble_verify.word_diff(pending["markdown"], second_markdown)
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
    Pages already approved before cancelling stay in the database.
    Deliberately does NOT delete this book's durable source (see
    sources.py) - unlike _advance()'s "done" branch, cancelling mid-review
    is exactly the situation resume() exists for."""
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


def resume(source_filename: str, total_pages: int, api_key: str = None) -> dict:
    """Continues a review session that was cancelled or simply abandoned
    (browser closed, server restarted) before every page was decided -
    loads the durable source PDF and settings sources.save() recorded
    during the original start() call, and converts+returns the page after
    wherever that session last got to, instead of starting over from page
    1. Raises FileNotFoundError if this book has nothing resumable (either
    it was never reviewed page-by-page at all, or it already finished -
    see _advance()'s "done" branch, which deletes this once there's
    nothing left to resume).

    `api_key` is optional and only matters for a vision-engine session:
    the original session's own key is never persisted (same "never saved
    server-side" rule every browser-supplied key in this app follows), so
    resuming one needs a key supplied again here, unless the backend's own
    environment already has one configured."""
    loaded = sources.load(source_filename, total_pages)
    if loaded is None:
        raise FileNotFoundError(
            f"No resumable review session found for {source_filename!r} - it was either "
            "never reviewed page-by-page, or already finished.")
    saved_pdf_path, settings = loaded

    # review.py always takes ownership of (and eventually deletes) a
    # session's pdf_path - operate on a throwaway copy so the durable
    # original survives in case this resumed session is cancelled again.
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    shutil.copy(saved_pdf_path, tmp_path)

    session_id = uuid.uuid4().hex
    session = {
        "session_id": session_id,
        "pdf_path": tmp_path,
        "filename": source_filename,
        "engine": settings["engine"],
        "provider": settings.get("provider"),
        "model": settings.get("model"),
        "langs": settings.get("langs"),
        "api_key": api_key,
        "auto_validate": bool(settings.get("auto_validate", False)),
        "ensemble_verify": bool(settings.get("ensemble_verify", False)),
        "category": settings.get("category"),
        "total_pages": total_pages,
        "current_index": None,
        "pending": None,
        "saved_chunk_ids": [],
        "log": [],
    }
    next_index = settings.get("next_index", 0)
    _log(session, f"Resuming: {source_filename} from page {next_index + 1}/{total_pages} "
                  f"(engine={session['engine']})")

    try:
        page = _convert_one_page(session, next_index)  # may raise - session not registered below if so
    except Exception as e:
        os.unlink(tmp_path)
        _log(session, f"ERROR converting page {next_index + 1}/{total_pages}: {e}")
        raise
    session["current_index"] = next_index
    session["pending"] = page
    _log(session, f"Page {next_index + 1}/{total_pages} ready for review")
    with _lock:
        _sessions[session_id] = session

    return {"session_id": session_id, "total_pages": total_pages, "page": page, "log": session["log"]}
