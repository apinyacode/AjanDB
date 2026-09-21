import os
import shutil
import time

import pytest

from backend import db, review, sources


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.sqlite3")
    real_get_connection = db.get_connection  # capture before patching to avoid self-reference
    monkeypatch.setattr(review.db, "get_connection", lambda: real_get_connection(db_path))
    # review._sessions is process-global state, not scoped to a connection or
    # tmp_path - a test that leaves a session unresolved (e.g. one exercising
    # an error path) would otherwise leak it into the next test.
    review._sessions.clear()
    yield
    review._sessions.clear()


def _session_pdf(tmp_path, name="upload.pdf"):
    # review.start() takes ownership of (and deletes) the path it's given,
    # so each test needs its own throwaway copy of the shared fixture PDF.
    import make_test_pdf

    built = make_test_pdf.build(str(tmp_path / "_fixture.pdf"))
    dest = str(tmp_path / name)
    shutil.copy(built, dest)
    return dest


def test_start_converts_page_one_and_saves_nothing_yet(tmp_path):
    result = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")

    assert result["total_pages"] == 3
    page = result["page"]
    assert page["page_number"] == 1
    assert "Field Visit Notes" in page["markdown"]
    assert page["image_base64"]
    assert page["flagged_snippets"] == []  # page 1 is all born-digital text
    assert page["typos"] == []  # page 1's Thai text is all correctly spelled
    assert result["log"] == [
        "Started: book.pdf (3 page(s), engine=classical)",
        "Converting page 1/3...",
        "Page 1/3 ready for review",
    ]

    conn = db.get_connection()
    try:
        assert db.list_documents(conn) == []
    finally:
        conn.close()


def test_approve_saves_immediately_and_returns_next_page(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")

    result = review.approve(started["session_id"])
    assert result["done"] is False
    assert result["page"]["page_number"] == 2
    assert result["total_saved"] == 1
    assert result["log"][-3:] == [
        "Saved page 1/3 (chunk id 1)",
        "Converting page 2/3...",
        "Page 2/3 ready for review",
    ]

    conn = db.get_connection()
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert len(docs) == 1
    assert docs[0]["page_number"] == 1
    assert docs[0]["total_pages"] == 3


def test_approve_can_override_markdown_and_needs_review(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    review.approve(started["session_id"], markdown="edited text", needs_review=True)

    conn = db.get_connection()
    try:
        doc = db.get_document(conn, 1)
    finally:
        conn.close()
    assert doc["markdown"] == "edited text"
    assert doc["needs_review"] == 1


def test_skip_advances_without_saving(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    result = review.skip(started["session_id"])

    assert result["done"] is False
    assert result["page"]["page_number"] == 2
    assert result["total_saved"] == 0
    assert "Skipped page 1/3 (not saved)" in result["log"]

    conn = db.get_connection()
    try:
        assert db.list_documents(conn) == []
    finally:
        conn.close()


def test_completes_after_last_page_and_cleans_up_temp_file(tmp_path):
    session_pdf = _session_pdf(tmp_path)
    started = review.start(session_pdf, "book.pdf", langs="eng+tha")
    session_id = started["session_id"]

    assert review.approve(session_id)["done"] is False
    assert review.approve(session_id)["done"] is False
    result = review.approve(session_id)

    assert result["done"] is True
    assert result["total_saved"] == 3
    assert result["category"] == "Uncategorized"  # no API key in the test environment
    assert result["log"][-1] == "Finished: 3 page(s) saved"
    assert not os.path.exists(session_pdf)
    with pytest.raises(KeyError):
        review._get_session(session_id)


def test_page_with_flagged_content_reports_flagged_snippets(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    session_id = started["session_id"]
    review.approve(session_id)  # page 1 -> page 2
    result = review.approve(session_id)  # page 2 -> page 3 (has simulated handwriting)

    page3 = result["page"]
    assert page3["page_number"] == 3
    assert page3["flagged_snippets"]
    assert all(s in page3["markdown"] for s in page3["flagged_snippets"])


def test_explicit_category_is_used_for_every_page(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha", category="Field Notes")
    session_id = started["session_id"]
    review.approve(session_id)
    review.approve(session_id)
    review.approve(session_id)

    conn = db.get_connection()
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert all(d["category"] == "Field Notes" for d in docs)


def test_approve_after_session_finished_raises_keyerror(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    session_id = started["session_id"]
    review.approve(session_id)
    review.approve(session_id)
    review.approve(session_id)  # last page -> session cleaned up

    with pytest.raises(KeyError):
        review.approve(session_id)


def test_retry_recovers_from_a_failed_next_page_conversion(tmp_path, monkeypatch):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    session_id = started["session_id"]

    real_convert = review._convert_one_page
    calls = {"n": 0}

    def flaky(session, page_index):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated OCR failure")
        return real_convert(session, page_index)

    monkeypatch.setattr(review, "_convert_one_page", flaky)

    with pytest.raises(RuntimeError):
        review.approve(session_id)  # page 1 saves fine; converting page 2 fails

    conn = db.get_connection()
    try:
        assert len(db.list_documents(conn)) == 1  # page 1's save was not lost
    finally:
        conn.close()

    # the failure itself is recorded in the session's log even though
    # approve() raised rather than returning a response
    failed_log = review._get_session(session_id)["log"]
    assert failed_log[-2:] == [
        "Converting page 2/3...",
        "ERROR converting page 2/3: simulated OCR failure",
    ]

    with pytest.raises(RuntimeError):
        review.approve(session_id)  # nothing pending - must not re-save or skip

    result = review.retry(session_id)
    assert result["done"] is False
    assert result["page"]["page_number"] == 2
    assert result["log"][-2:] == [
        "Converting page 2/3...",
        "Page 2/3 ready for review",
    ]


def test_retry_refuses_when_a_page_is_already_pending(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    with pytest.raises(RuntimeError):
        review.retry(started["session_id"])


def test_cancel_keeps_already_saved_pages_and_cleans_up(tmp_path):
    session_pdf = _session_pdf(tmp_path)
    started = review.start(session_pdf, "book.pdf", langs="eng+tha")
    session_id = started["session_id"]

    review.approve(session_id)  # save page 1; page 2 becomes pending
    result = review.cancel(session_id)

    assert result["total_saved"] == 1
    assert result["log"][-1] == "Cancelled after page 2/3 (1 page(s) saved)"
    assert not os.path.exists(session_pdf)
    with pytest.raises(KeyError):
        review._get_session(session_id)

    conn = db.get_connection()
    try:
        assert len(db.list_documents(conn)) == 1
    finally:
        conn.close()


def test_unknown_session_id_raises_keyerror():
    with pytest.raises(KeyError):
        review._get_session("nope")


def test_start_cleans_up_nothing_itself_on_failure_but_does_not_register_session(tmp_path, monkeypatch):
    # start() must not leak a session into _sessions if converting page 1 fails -
    # the caller (main.py) is responsible for deleting the temp file in that case.
    def boom(session, page_index):
        raise RuntimeError("boom")

    monkeypatch.setattr(review, "_convert_one_page", boom)

    with pytest.raises(RuntimeError):
        review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")

    assert review._sessions == {}


# --- Second-opinion verification (/api/review/{id}/verify) ---
# (word_diff itself - the shared diff primitive verify_second_opinion and
# the cross-checks above are built on - now lives in ensemble_verify.py,
# see test_ensemble_verify.py for its own tests.)

def test_verify_second_opinion_returns_diff_against_pending_page(tmp_path, monkeypatch):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    session_id = started["session_id"]
    original_markdown = started["page"]["markdown"]

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": original_markdown.replace("Notes", "NOTES"),
                  "confidence": 90.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    result = review.verify_second_opinion(session_id, provider="openai", api_key="fake-key")

    assert result["provider"] == "openai"
    assert result["original_markdown"] == original_markdown
    assert "NOTES" in result["second_markdown"]
    assert 0 < result["agreement_ratio"] < 100
    assert any(s["tag"] != "equal" for s in result["diff"])
    assert "Verification agreement" in result["log"][-1]

    # doesn't touch approve/skip state - the session is untouched, still on page 1
    assert review._get_session(session_id)["current_index"] == 0
    assert review._get_session(session_id)["pending"] is not None


def test_verify_second_opinion_raises_when_no_page_pending(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    session_id = started["session_id"]
    review.approve(session_id)
    review.approve(session_id)
    review.approve(session_id)  # last page -> session finishes and is cleaned up

    with pytest.raises(KeyError):
        review.verify_second_opinion(session_id, provider="anthropic", api_key="fake")


def test_verify_second_opinion_propagates_missing_api_key_error(tmp_path, monkeypatch):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    session_id = started["session_id"]
    # page 1 is born-digital text (no OCR needed at all, so no API key check
    # would fire) - advance to page 2, a genuinely scanned page, to test this.
    review.approve(session_id)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        review.verify_second_opinion(session_id, provider="anthropic", api_key=None)

    # session must still be usable afterwards - verification failing shouldn't break review
    session = review._get_session(session_id)
    assert session["pending"] is not None


# --- Phase 3: automatic multi-signal "likely wrong" flagging ---

def test_dedup_flags_removes_exact_duplicates_preserving_order():
    assert review._dedup_flags(["a", "b", "a", "c", "b", ""]) == ["a", "b", "c"]


def test_auto_validate_defaults_off_even_with_an_environment_key_configured(tmp_path, monkeypatch):
    """auto_validate is an explicit opt-in (see start()'s docstring) - a
    session that doesn't pass it must never run the cross-checks, even when
    an env key that would otherwise satisfy them is present."""
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        if provider == "openai":
            return [{"page_number": 1, "markdown": "The cat sat on the mat",
                      "confidence": 96.0, "needs_review": False, "flagged_snippets": []}]
        return [{"page_number": 1, "markdown": "The dog sat on the mat",
                  "confidence": 97.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key")  # auto_validate omitted
    page = started["page"]

    assert page["flagged_snippets"] == []
    assert page["needs_review"] is False


def test_auto_cross_check_flags_disagreement_even_with_high_self_reported_confidence(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        if provider == "openai":
            return [{"page_number": 1, "markdown": "The cat sat on the mat",
                      "confidence": 96.0, "needs_review": False, "flagged_snippets": []}]
        return [{"page_number": 1, "markdown": "The dog sat on the mat",
                  "confidence": 97.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key", auto_validate=True)
    page = started["page"]

    assert page["confidence"] == 97.0  # each provider's own self-reported confidence was high
    assert any("dog" in s for s in page["flagged_snippets"])
    assert page["needs_review"] is True


def test_auto_cross_check_is_silently_skipped_without_an_environment_key(tmp_path, monkeypatch):
    from pipeline import ocr as pdf_ocr

    # avoid the (unrelated) free Tesseract cross-check also firing here, so
    # this test isolates the "no env key" skip path specifically
    monkeypatch.setattr(
        pdf_ocr, "process_scanned_page",
        lambda png_bytes, langs=None: {
            "lines": [{"text": "Some transcribed text", "confidence": 90, "bbox": (0, 0, 1, 1)}],
            "pictures": [], "processed": None,
        })

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": "Some transcribed text",
                  "confidence": 90.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key", auto_validate=True)
    page = started["page"]

    assert page["flagged_snippets"] == []  # no env key configured -> cross-check never ran
    assert page["needs_review"] is False


def test_tesseract_cross_check_flags_disagreement_for_vision_engine_pages(tmp_path, monkeypatch):
    from pipeline import ocr as pdf_ocr

    monkeypatch.setattr(
        pdf_ocr, "process_scanned_page",
        lambda png_bytes, langs=None: {
            "lines": [{"text": "completely different tesseract output", "confidence": 80, "bbox": (0, 0, 1, 1)}],
            "pictures": [], "processed": None,
        })

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": "vision model transcription here",
                  "confidence": 92.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key", auto_validate=True)
    page = started["page"]

    assert page["flagged_snippets"]  # Tesseract's disagreeing text got flagged, free/no API key needed
    assert page["needs_review"] is True


def test_tesseract_cross_check_does_not_run_for_classical_engine_pages(tmp_path, monkeypatch):
    # page 2 of the fixture genuinely needs classical OCR - pdf_to_markdown
    # itself calls pipeline.ocr.process_scanned_page as part of its normal,
    # already-existing job, so asserting on *that* call wouldn't isolate
    # anything; assert directly that review.py's own cross-check wrapper is
    # never invoked for a classical-engine session instead.
    calls = []
    monkeypatch.setattr(review, "_tesseract_cross_check_flags", lambda *a, **k: calls.append(1) or [])

    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha", auto_validate=True)
    review.approve(started["session_id"])

    assert calls == []


def test_openai_logprob_check_flags_low_confidence_span(tmp_path, monkeypatch):
    from pipeline import ocr as pdf_ocr
    from pipeline import vision_ocr

    # keep the (unrelated) free Tesseract cross-check from also firing here,
    # so this test isolates the logprob signal specifically
    monkeypatch.setattr(
        pdf_ocr, "process_scanned_page",
        lambda png_bytes, langs=None: {
            "lines": [{"text": "some garbled region of text", "confidence": 90, "bbox": (0, 0, 1, 1)}],
            "pictures": [], "processed": None,
        })

    def fake_extract_logprobs(png_bytes, model=None, api_key=None):
        blocks = [{"type": "text", "text": "garbled region", "bbox": [0, 0, 1, 1], "confidence": 90}]
        raw = '{"blocks": [{"type": "text", "text": "garbled region"}]}'
        # char-level "tokens" so concatenation reconstructs `raw` exactly at
        # the right offsets - low logprob only for the chars actually inside
        # the "garbled region" span, normal logprob everywhere else.
        span_start = raw.index("garbled region")
        span_end = span_start + len("garbled region")
        token_logprobs = [
            (ch, -3.0 if span_start <= i < span_end else -0.01)
            for i, ch in enumerate(raw)
        ]
        return blocks, raw, token_logprobs

    monkeypatch.setattr(vision_ocr, "extract_page_with_vision_logprobs", fake_extract_logprobs)

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": "some garbled region of text",
                  "confidence": 88.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="openai", api_key="fake-openai-key", auto_validate=True)
    page = started["page"]

    assert "garbled region" in page["flagged_snippets"]
    assert page["needs_review"] is True


def test_openai_logprob_check_does_not_run_for_anthropic_provider(tmp_path, monkeypatch):
    from pipeline import ocr as pdf_ocr
    from pipeline import vision_ocr

    monkeypatch.setattr(
        pdf_ocr, "process_scanned_page",
        lambda png_bytes, langs=None: {
            "lines": [{"text": "vision model transcription here", "confidence": 90, "bbox": (0, 0, 1, 1)}],
            "pictures": [], "processed": None,
        })
    calls = []
    monkeypatch.setattr(
        vision_ocr, "extract_page_with_vision_logprobs",
        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("should not be called")))

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": "vision model transcription here",
                  "confidence": 92.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key", auto_validate=True)

    assert calls == []
    assert started["page"]["flagged_snippets"] == []


def test_multiple_flag_sources_on_the_same_span_are_unioned_without_duplicates(tmp_path, monkeypatch):
    from pipeline import ocr as pdf_ocr

    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    monkeypatch.setattr(
        pdf_ocr, "process_scanned_page",
        lambda png_bytes, langs=None: {
            "lines": [{"text": "shaky text over here", "confidence": 80, "bbox": (0, 0, 1, 1)}],
            "pictures": [], "processed": None,
        })

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        if provider == "openai":
            return [{"page_number": 1, "markdown": "shaky text over here",
                      "confidence": 95.0, "needs_review": False, "flagged_snippets": []}]
        return [{"page_number": 1, "markdown": "unclear text over here",
                  "confidence": 90.0, "needs_review": False, "flagged_snippets": ["unclear"]}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key", auto_validate=True)
    page = started["page"]

    # "unclear" was independently flagged by the original per-line confidence
    # check, the cross-provider diff, AND the Tesseract diff - the union
    # must still list it exactly once, and the highlight renderer (app.js)
    # only ever needs one entry per distinct span to render it correctly.
    assert page["flagged_snippets"].count("unclear") == 1
    assert len(page["flagged_snippets"]) == len(set(page["flagged_snippets"]))
    assert page["needs_review"] is True


# --- Ensemble verification (own opt-in, independent of auto_validate) ---

def test_ensemble_verify_defaults_off_even_with_both_env_keys_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": f"text from {provider}", "confidence": 90.0,
                  "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key")  # ensemble_verify omitted
    page = started["page"]

    assert page["ensemble_diff"] is None
    assert page["agreement_pct"] is None


def test_ensemble_verification_diffs_vision_session_against_the_other_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    calls = []

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        calls.append(provider)
        if provider == "openai":
            return [{"page_number": 1, "markdown": "The cat sat on the mat",
                      "confidence": 96.0, "needs_review": False, "flagged_snippets": []}]
        return [{"page_number": 1, "markdown": "The dog sat on the mat",
                  "confidence": 97.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key", ensemble_verify=True)
    page = started["page"]

    # the session's own primary conversion (anthropic) is reused as one
    # ensemble member - only openai should have been called a second time.
    assert calls == ["anthropic"] or calls == ["anthropic", "openai"]  # order across threads isn't fixed
    assert sorted(calls) == ["anthropic", "openai"]
    assert calls.count("anthropic") == 1  # never called twice for the same page

    assert page["agreement_pct"] is not None and page["agreement_pct"] < 100.0
    assert any(s["agreement"] == "none" for s in page["ensemble_diff"])
    disagreement = next(s for s in page["ensemble_diff"] if s["agreement"] == "none")
    assert disagreement["providers"] == {"anthropic": "dog", "openai": "cat"}
    assert page["needs_review"] is True
    assert page["markdown"] == "The dog sat on the mat"  # unaffected - still the primary conversion


def test_ensemble_verification_calls_both_providers_fresh_for_classical_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    calls = []

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        calls.append(provider)
        return [{"page_number": 1, "markdown": f"ensemble text from {provider}", "confidence": 90.0,
                  "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    # page 2 of the fixture genuinely needs classical OCR (page 1 is
    # born-digital, confidence 100 - nothing for ensemble mode to check).
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha", ensemble_verify=True)
    review.approve(started["session_id"])
    page = review._sessions[started["session_id"]]["pending"]

    # classical engine never called either provider on its own - both are
    # genuinely fresh calls here, unlike the vision-engine case above.
    assert sorted(calls) == ["anthropic", "openai"]
    assert page["agreement_pct"] is not None
    assert page["ensemble_diff"] is not None


def test_ensemble_verification_skipped_without_required_api_keys(tmp_path, monkeypatch):
    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": "text", "confidence": 90.0,
                  "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="anthropic", api_key="fake-anthropic-key", ensemble_verify=True)
    page = started["page"]

    assert page["ensemble_diff"] is None
    assert page["agreement_pct"] is None
    assert any("Ensemble verification skipped" in line for line in started["log"])


def test_ensemble_verification_skipped_for_native_confidence_100_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    calls = []
    monkeypatch.setattr(
        review.convert, "pdf_to_markdown_vision",
        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("should not be called")))

    # page 1 of the fixture is born-digital (confidence 100) - nothing for
    # ensemble mode (or any other cross-check) to add.
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha", ensemble_verify=True)
    page = started["page"]

    assert calls == []
    assert page["ensemble_diff"] is None
    assert page["agreement_pct"] is None


def test_ensemble_verify_setting_survives_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": f"text from {provider}", "confidence": 90.0,
                  "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision", provider="anthropic",
                            api_key="fake-anthropic-key", ensemble_verify=True)
    review.cancel(started["session_id"])

    resumed = review.resume("book.pdf", 3, api_key="fake-anthropic-key")

    assert resumed["page"]["ensemble_diff"] is not None
    assert resumed["page"]["agreement_pct"] is not None
    review.cancel(resumed["session_id"])


# --- Thai dual-tokenizer divergence on disputed ensemble spans ---

def test_annotate_thai_divergence_flags_highly_diverging_thai_spans(monkeypatch):
    monkeypatch.setattr(review.spellcheck, "contains_thai", lambda text: True)
    monkeypatch.setattr(review.spellcheck, "tokenizer_divergence", lambda text: 40.0)  # below threshold

    spans = [{"providers": {"anthropic": "บาง คำ", "openai": "อีก คำ"}, "agreement": "none"}]
    review._annotate_thai_divergence(spans)

    assert spans[0]["high_divergence"] is True


def test_annotate_thai_divergence_leaves_low_divergence_spans_unflagged(monkeypatch):
    monkeypatch.setattr(review.spellcheck, "contains_thai", lambda text: True)
    monkeypatch.setattr(review.spellcheck, "tokenizer_divergence", lambda text: 95.0)  # well above threshold

    spans = [{"providers": {"anthropic": "text", "openai": "text2"}, "agreement": "none"}]
    review._annotate_thai_divergence(spans)

    assert "high_divergence" not in spans[0]


def test_annotate_thai_divergence_skips_non_thai_spans(monkeypatch):
    called = []
    monkeypatch.setattr(review.spellcheck, "contains_thai", lambda text: False)
    monkeypatch.setattr(review.spellcheck, "tokenizer_divergence", lambda text: called.append(1) or 0.0)

    spans = [{"providers": {"anthropic": "English text", "openai": "Other text"}, "agreement": "none"}]
    review._annotate_thai_divergence(spans)

    assert called == []  # never even computed for non-Thai text
    assert "high_divergence" not in spans[0]


def test_annotate_thai_divergence_skips_full_agreement_spans():
    spans = [{"text": "agreed text", "agreement": "full"}]
    review._annotate_thai_divergence(spans)  # must not raise - no "providers" key on this span
    assert "high_divergence" not in spans[0]


def test_cross_checks_run_concurrently_not_sequentially(monkeypatch):
    """Regression test for the tunnel-timeout bug this was built to fix:
    stacking the cross-provider check and the logprob check one after
    another (a fixed 0.3s "API call" each) must NOT take ~0.6s total - they
    need to run at the same time."""
    monkeypatch.setattr(review, "_CROSS_CHECK_TIMEOUT_SECONDS", 5)

    def slow_job():
        time.sleep(0.3)
        return ["flag-from-slow-job"]

    started = time.monotonic()
    results = review._run_cross_checks_with_timeout([slow_job, slow_job])
    elapsed = time.monotonic() - started

    assert results == [["flag-from-slow-job"], ["flag-from-slow-job"]]
    assert elapsed < 0.55  # concurrent: ~0.3s: sequential would be ~0.6s+


def test_cross_check_timeout_abandons_a_slow_job_without_blocking(monkeypatch):
    monkeypatch.setattr(review, "_CROSS_CHECK_TIMEOUT_SECONDS", 0.2)

    def hangs_forever():
        time.sleep(30)
        return ["should never see this"]

    started = time.monotonic()
    results = review._run_cross_checks_with_timeout([hangs_forever])
    elapsed = time.monotonic() - started

    assert results == [[]]  # abandoned job contributes no flags
    assert elapsed < 5  # gave up around the timeout, did not wait for the 30s sleep


def test_cross_check_exception_contributes_no_flags_without_failing_the_page():
    def raises():
        raise RuntimeError("simulated provider error")

    assert review._run_cross_checks_with_timeout([raises]) == [[]]


# --- Resuming an incomplete review session (see sources.py) ---

def test_start_saves_a_resumable_source(tmp_path):
    review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    assert sources.exists("book.pdf", 3)
    _, settings = sources.load("book.pdf", 3)
    assert settings["next_index"] == 0
    assert settings["langs"] == "eng+tha"


def test_cancel_leaves_the_source_resumable_at_the_right_page(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    review.approve(started["session_id"])  # page 1 -> page 2

    review.cancel(started["session_id"])

    assert sources.exists("book.pdf", 3)
    _, settings = sources.load("book.pdf", 3)
    assert settings["next_index"] == 1  # stopped having just moved onto page 2


def test_finishing_a_review_deletes_the_resumable_source(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    session_id = started["session_id"]
    review.approve(session_id)  # page 1 -> 2
    review.approve(session_id)  # page 2 -> 3
    review.approve(session_id)  # page 3 -> done

    assert not sources.exists("book.pdf", 3)


def test_resume_raises_when_nothing_is_resumable(tmp_path):
    with pytest.raises(FileNotFoundError):
        review.resume("never-reviewed.pdf", 3)


def test_resume_continues_from_the_last_reached_page(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    review.approve(started["session_id"])  # page 1 saved, now on page 2
    review.cancel(started["session_id"])

    result = review.resume("book.pdf", 3)

    assert result["page"]["page_number"] == 2  # not page 1 again
    assert result["total_pages"] == 3
    assert "Resuming: book.pdf from page 2/3" in result["log"][0]


def test_resume_preserves_the_original_engine_and_provider(tmp_path, monkeypatch):
    captured_providers = []

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        captured_providers.append(provider)
        return [{"page_number": 1, "markdown": "vision text", "confidence": 90.0,
                  "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)
    started = review.start(_session_pdf(tmp_path), "book.pdf", engine="vision",
                            provider="openai", api_key="fake-key")
    review.cancel(started["session_id"])

    result = review.resume("book.pdf", 3, api_key="fake-key-again")

    assert captured_providers[-1] == "openai"  # resumed with the same provider, not the default
    assert result["page"]["markdown"] == "vision text"


def test_resumed_session_can_complete_normally_and_cleans_up(tmp_path):
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha")
    review.approve(started["session_id"])  # page 1 saved
    review.cancel(started["session_id"])

    resumed = review.resume("book.pdf", 3)
    session_id = resumed["session_id"]
    review.approve(session_id)  # page 2 saved -> page 3
    result = review.approve(session_id)  # page 3 saved -> done

    assert result["done"] is True
    assert result["total_saved"] == 2  # this resumed session's own approvals
    assert not sources.exists("book.pdf", 3)

    conn = db.get_connection()
    try:
        book = [b for b in db.list_books(conn) if b["source_filename"] == "book.pdf"][0]
    finally:
        conn.close()
    assert book["pages_stored"] == 3  # page 1 (before cancel) + pages 2-3 (after resume)


def test_resume_updates_category_decided_partway_through_the_original_session(tmp_path):
    # category is None until the first approve() auto-suggests one - a
    # resume must see that decision, not silently re-guess (and possibly
    # pick something different) for the remaining pages of the same book.
    started = review.start(_session_pdf(tmp_path), "book.pdf", langs="eng+tha",
                            category="Field Notes")
    review.approve(started["session_id"])
    review.cancel(started["session_id"])

    _, settings = sources.load("book.pdf", 3)
    assert settings["category"] == "Field Notes"

    result = review.resume("book.pdf", 3)
    session = review._get_session(result["session_id"])
    assert session["category"] == "Field Notes"
