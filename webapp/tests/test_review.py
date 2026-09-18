import os
import shutil

import pytest

from backend import db, review


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
