import sys

import pytest

from backend import db
from scripts import chunked_upload


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    # chunked_upload.STATE_DIR defaults to a real path under webapp/data/ -
    # tests must never touch that directory, so every test gets its own
    # tmp_path-scoped state dir instead.
    monkeypatch.setattr(chunked_upload, "STATE_DIR", str(tmp_path / "chunked_upload_state"))


def _make_pdf(tmp_path, name="sample.pdf"):
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "pdf_to_docx_pipeline"))
    import make_test_pdf
    return make_test_pdf.build(str(tmp_path / name))


def test_process_pdf_chunks_across_multiple_batches(tmp_path):
    pdf_path = _make_pdf(tmp_path)
    db_path = str(tmp_path / "test.sqlite3")

    result = chunked_upload.process_pdf(
        pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha")

    assert result["total_pages"] == 3
    assert result["ranges_done"] == [(0, 1), (1, 2), (2, 3)]
    assert result["ranges_failed"] == []
    assert len(result["chunk_ids"]) == 3

    conn = db.get_connection(db_path)
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert len(docs) == 3
    assert sorted(d["page_number"] for d in docs) == [1, 2, 3]
    assert all(d["total_pages"] == 3 for d in docs)
    assert all(d["source_filename"] == "sample.pdf" for d in docs)


def test_process_pdf_page_numbers_are_offset_within_multi_page_batches(tmp_path):
    pdf_path = _make_pdf(tmp_path)
    db_path = str(tmp_path / "test.sqlite3")

    chunked_upload.process_pdf(pdf_path, db_path=db_path, chunk_size=2, langs="eng+tha")

    conn = db.get_connection(db_path)
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert sorted(d["page_number"] for d in docs) == [1, 2, 3]


def test_process_pdf_resume_skips_already_done_ranges(tmp_path):
    pdf_path = _make_pdf(tmp_path)
    db_path = str(tmp_path / "test.sqlite3")

    chunked_upload.process_pdf(pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha")

    messages = []
    result = chunked_upload.process_pdf(
        pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha",
        resume=True, progress=messages.append)

    assert result["chunk_ids"] == []
    assert all("already done" in m for m in messages)

    conn = db.get_connection(db_path)
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert len(docs) == 3  # unchanged - nothing re-inserted


def test_process_pdf_without_resume_reprocesses_everything(tmp_path):
    pdf_path = _make_pdf(tmp_path)
    db_path = str(tmp_path / "test.sqlite3")

    chunked_upload.process_pdf(pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha")
    chunked_upload.process_pdf(pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha")

    conn = db.get_connection(db_path)
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert len(docs) == 6  # no --resume -> second run reprocesses and duplicates


def test_process_pdf_explicit_category_skips_suggestion(tmp_path, monkeypatch):
    pdf_path = _make_pdf(tmp_path)
    db_path = str(tmp_path / "test.sqlite3")

    def boom(*args, **kwargs):
        raise AssertionError("should not be called when --category is given")

    monkeypatch.setattr(chunked_upload.categorize, "suggest_category", boom)

    chunked_upload.process_pdf(
        pdf_path, db_path=db_path, chunk_size=5, langs="eng+tha", category="My Book")

    conn = db.get_connection(db_path)
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert all(d["category"] == "My Book" for d in docs)


def test_process_pdf_reuses_existing_category_for_same_filename(tmp_path):
    pdf_path = _make_pdf(tmp_path)
    db_path = str(tmp_path / "test.sqlite3")

    chunked_upload.process_pdf(
        pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha", category="Field Notes")

    # Second run (e.g. a --resume after clearing local state) with no
    # --category should pick up "Field Notes" from the existing rows
    # instead of falling back to auto-suggestion/"Uncategorized".
    result = chunked_upload.process_pdf(
        pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha")

    conn = db.get_connection(db_path)
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert len(result["chunk_ids"]) == 3
    assert all(d["category"] == "Field Notes" for d in docs)


def test_process_pdf_continues_past_failed_range_by_default(tmp_path):
    pdf_path = _make_pdf(tmp_path)
    db_path = str(tmp_path / "test.sqlite3")

    calls = {"n": 0}
    real_pdf_to_markdown = chunked_upload.convert.pdf_to_markdown

    def flaky(pdf_path_arg, langs=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated OCR failure")
        return real_pdf_to_markdown(pdf_path_arg, langs=langs)

    import unittest.mock
    with unittest.mock.patch.object(chunked_upload.convert, "pdf_to_markdown", flaky):
        result = chunked_upload.process_pdf(
            pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha")

    assert len(result["ranges_failed"]) == 1
    assert result["ranges_failed"][0][:2] == (1, 2)
    assert len(result["ranges_done"]) == 2


def test_process_pdf_stop_on_error_raises_and_halts(tmp_path):
    pdf_path = _make_pdf(tmp_path)
    db_path = str(tmp_path / "test.sqlite3")

    import unittest.mock

    def always_fails(pdf_path_arg, langs=None):
        raise RuntimeError("simulated OCR failure")

    with unittest.mock.patch.object(chunked_upload.convert, "pdf_to_markdown", always_fails):
        try:
            chunked_upload.process_pdf(
                pdf_path, db_path=db_path, chunk_size=1, langs="eng+tha", stop_on_error=True)
            assert False, "expected RuntimeError to propagate"
        except RuntimeError:
            pass

    conn = db.get_connection(db_path)
    try:
        docs = db.list_documents(conn)
    finally:
        conn.close()
    assert docs == []  # first range failed before any insert happened
