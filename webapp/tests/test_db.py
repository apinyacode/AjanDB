from backend import db


def _tmp_conn(tmp_path):
    return db.get_connection(str(tmp_path / "test.sqlite3"))


def _one_chunk(markdown, confidence=None, needs_review=False):
    return [{"page_number": 1, "markdown": markdown, "confidence": confidence,
             "needs_review": needs_review}]


def test_insert_and_get_document(tmp_path):
    conn = _tmp_conn(tmp_path)
    ids = db.insert_chunks(conn, "notes.md", "md", "Uncategorized", _one_chunk("# Hello\nWorld"))
    doc = db.get_document(conn, ids[0])
    assert doc["source_filename"] == "notes.md"
    assert doc["source_type"] == "md"
    assert doc["page_number"] == 1
    assert doc["total_pages"] == 1
    assert doc["category"] == "Uncategorized"
    assert "Hello" in doc["markdown"]
    assert doc["confidence"] is None
    assert doc["needs_review"] == 0


def test_insert_chunks_stores_confidence_and_review_flag(tmp_path):
    conn = _tmp_conn(tmp_path)
    ids = db.insert_chunks(
        conn, "scan.pdf", "pdf", "Field Notes",
        _one_chunk("garbled text", confidence=42.5, needs_review=True),
    )
    doc = db.get_document(conn, ids[0])
    assert doc["confidence"] == 42.5
    assert doc["needs_review"] == 1


def test_insert_chunks_shares_total_pages_across_one_upload(tmp_path):
    conn = _tmp_conn(tmp_path)
    chunks = [
        {"page_number": 1, "markdown": "page one", "confidence": 90.0, "needs_review": False},
        {"page_number": 2, "markdown": "page two", "confidence": 60.0, "needs_review": True},
    ]
    ids = db.insert_chunks(conn, "book.pdf", "pdf", "Book", chunks)
    doc1, doc2 = db.get_document(conn, ids[0]), db.get_document(conn, ids[1])
    assert doc1["total_pages"] == 2 and doc2["total_pages"] == 2
    assert doc1["page_number"] == 1 and doc2["page_number"] == 2
    assert doc1["source_filename"] == doc2["source_filename"] == "book.pdf"


def test_get_document_returns_none_for_missing_id(tmp_path):
    conn = _tmp_conn(tmp_path)
    assert db.get_document(conn, 999) is None


def test_search_finds_matching_document(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "parenting.md", "md", "Parenting",
                      _one_chunk("Tips on how to raise a child with patience."))
    db.insert_chunks(conn, "cooking.md", "md", "Cooking", _one_chunk("A recipe for pasta."))
    results = db.search(conn, "child")
    assert len(results) == 1
    assert results[0]["source_filename"] == "parenting.md"
    assert results[0]["category"] == "Parenting"
    assert "<mark>" in results[0]["snippet"]


def test_search_returns_empty_list_for_no_matches(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "cooking.md", "md", "Cooking", _one_chunk("A recipe for pasta."))
    assert db.search(conn, "spaceship") == []


def test_search_with_special_characters_does_not_raise(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "notes.md", "md", "Notes", _one_chunk("Some content here."))
    # FTS5 treats "*, -, (, )" etc. as syntax - the query sanitizer must
    # neutralise that rather than let sqlite3.OperationalError propagate.
    assert db.search(conn, 'weird" query (with) -syntax*') == []


def test_search_matches_on_category(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "notes.md", "md", "Dharma Talks", _one_chunk("Some content here."))
    results = db.search(conn, "Dharma")
    assert len(results) == 1


def test_list_documents_orders_newest_first(tmp_path):
    conn = _tmp_conn(tmp_path)
    ids1 = db.insert_chunks(conn, "first.md", "md", "Notes", _one_chunk("one"))
    ids2 = db.insert_chunks(conn, "second.md", "md", "Notes", _one_chunk("two"))
    docs = db.list_documents(conn)
    assert docs[0]["id"] == ids2[0]
    assert docs[1]["id"] == ids1[0]


def test_list_books_groups_one_upload_into_one_entry(tmp_path):
    conn = _tmp_conn(tmp_path)
    chunks = [
        {"page_number": 1, "markdown": "page one", "confidence": 90.0, "needs_review": False},
        {"page_number": 2, "markdown": "page two", "confidence": 60.0, "needs_review": True},
    ]
    db.insert_chunks(conn, "book.pdf", "pdf", "Dharma Talks", chunks)

    books = db.list_books(conn)
    assert len(books) == 1
    book = books[0]
    assert book["source_filename"] == "book.pdf"
    assert book["source_type"] == "pdf"
    assert book["total_pages"] == 2
    assert book["pages_stored"] == 2
    assert book["category"] == "Dharma Talks"
    assert book["needs_review_count"] == 1
    assert book["avg_confidence"] == 75.0


def test_list_books_groups_multi_batch_upload_into_one_entry(tmp_path):
    # scripts/chunked_upload.py and the page-by-page review flow each call
    # insert_chunks() once per batch/page for the SAME logical book, with a
    # different uploaded_at every time - grouping must not be fooled by that.
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "book.pdf", "pdf", "Field Notes",
                      [{"page_number": 1, "markdown": "p1", "confidence": 90.0, "needs_review": False}],
                      total_pages=3)
    db.insert_chunks(conn, "book.pdf", "pdf", "Field Notes",
                      [{"page_number": 2, "markdown": "p2", "confidence": 80.0, "needs_review": False}],
                      total_pages=3)
    db.insert_chunks(conn, "book.pdf", "pdf", "Field Notes",
                      [{"page_number": 3, "markdown": "p3", "confidence": 70.0, "needs_review": False}],
                      total_pages=3)

    books = db.list_books(conn)
    assert len(books) == 1
    assert books[0]["total_pages"] == 3
    assert books[0]["pages_stored"] == 3


def test_list_books_keeps_different_files_separate(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "a.md", "md", "Notes", _one_chunk("a"))
    db.insert_chunks(conn, "b.md", "md", "Notes", _one_chunk("b"))
    assert len(db.list_books(conn)) == 2


def test_list_books_avg_confidence_is_none_when_no_chunk_has_one(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "notes.md", "md", "Notes", _one_chunk("plain text"))
    assert db.list_books(conn)[0]["avg_confidence"] is None


def test_get_book_chunks_returns_ordered_pages(tmp_path):
    conn = _tmp_conn(tmp_path)
    chunks = [
        {"page_number": 2, "markdown": "second", "confidence": None, "needs_review": False},
        {"page_number": 1, "markdown": "first", "confidence": None, "needs_review": False},
    ]
    ids = db.insert_chunks(conn, "book.pdf", "pdf", "Notes", chunks)
    book_id = db.list_books(conn)[0]["id"]

    result = db.get_book_chunks(conn, book_id)
    assert [c["page_number"] for c in result] == [1, 2]
    assert [c["markdown"] for c in result] == ["first", "second"]
    assert book_id in ids


def test_get_book_chunks_returns_none_for_unknown_id(tmp_path):
    conn = _tmp_conn(tmp_path)
    assert db.get_book_chunks(conn, 999) is None


def test_reuploading_a_page_does_not_duplicate_or_double_count_it(tmp_path):
    # Re-running an upload for the same file (e.g. scripts/chunked_upload.py
    # without --resume) inserts a second row per page rather than replacing
    # the first. Browsing/exporting must show each page once - the current
    # version - not double it or sum stats across both copies.
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "book.pdf", "pdf", "Notes",
                      [{"page_number": 1, "markdown": "old text", "confidence": 50.0, "needs_review": True}],
                      total_pages=2)
    db.insert_chunks(conn, "book.pdf", "pdf", "Notes",
                      [{"page_number": 2, "markdown": "page two", "confidence": 90.0, "needs_review": False}],
                      total_pages=2)
    # re-upload page 1 - newer row, should supersede the first for that page
    db.insert_chunks(conn, "book.pdf", "pdf", "Notes",
                      [{"page_number": 1, "markdown": "new text", "confidence": 95.0, "needs_review": False}],
                      total_pages=2)

    books = db.list_books(conn)
    assert len(books) == 1
    book = books[0]
    assert book["pages_stored"] == 2  # not 3, even though 3 rows exist
    assert book["needs_review_count"] == 0  # the stale flagged page-1 row must not count
    assert book["avg_confidence"] == 92.5  # avg(95.0, 90.0), not the stale 50.0

    chunks = db.get_book_chunks(conn, book["id"])
    assert len(chunks) == 2  # not 3
    assert chunks[0]["markdown"] == "new text"  # latest wins, not the stale copy
    assert chunks[1]["markdown"] == "page two"


def test_list_books_ready_for_publish_defaults_false(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "book.pdf", "pdf", "Notes", _one_chunk("text"))
    assert db.list_books(conn)[0]["ready_for_publish"] == 0


def test_set_ready_for_publish_toggles_the_flag(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "book.pdf", "pdf", "Notes", _one_chunk("text"))
    book_id = db.list_books(conn)[0]["id"]

    db.set_ready_for_publish(conn, "book.pdf", 1, True)
    assert db.list_books(conn)[0]["ready_for_publish"] == 1

    db.set_ready_for_publish(conn, "book.pdf", 1, False)
    assert db.list_books(conn)[0]["ready_for_publish"] == 0
    assert db.list_books(conn)[0]["id"] == book_id  # unrelated to the book's own identity


def test_set_ready_for_publish_is_independent_per_book(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "a.md", "md", "Notes", _one_chunk("a"))
    db.insert_chunks(conn, "b.md", "md", "Notes", _one_chunk("b"))
    db.set_ready_for_publish(conn, "a.md", 1, True)

    books = {b["source_filename"]: b for b in db.list_books(conn)}
    assert books["a.md"]["ready_for_publish"] == 1
    assert books["b.md"]["ready_for_publish"] == 0


def test_delete_book_removes_all_its_chunks_and_flag(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "book.pdf", "pdf", "Notes",
                      [{"page_number": 1, "markdown": "p1", "confidence": 90.0, "needs_review": False},
                       {"page_number": 2, "markdown": "p2", "confidence": 90.0, "needs_review": False}])
    db.insert_chunks(conn, "other.md", "md", "Notes", _one_chunk("keep me"))
    book_id = [b for b in db.list_books(conn) if b["source_filename"] == "book.pdf"][0]["id"]
    db.set_ready_for_publish(conn, "book.pdf", 2, True)

    deleted = db.delete_book(conn, book_id)

    assert deleted == 2
    remaining = db.list_books(conn)
    assert len(remaining) == 1
    assert remaining[0]["source_filename"] == "other.md"
    # the flag row for the deleted book shouldn't linger as dead state either
    leftover_flags = conn.execute(
        "SELECT * FROM book_flags WHERE source_filename = 'book.pdf'").fetchall()
    assert leftover_flags == []


def test_delete_book_returns_zero_for_unknown_id(tmp_path):
    conn = _tmp_conn(tmp_path)
    assert db.delete_book(conn, 99999) == 0


def test_delete_book_also_removes_stray_duplicate_rows(tmp_path):
    # A re-uploaded page leaves an extra, superseded row (see the
    # re-uploading test above) - deleting the book must not leave that
    # orphaned row behind just because get_book_chunks()/list_books() don't
    # surface it.
    conn = _tmp_conn(tmp_path)
    db.insert_chunks(conn, "book.pdf", "pdf", "Notes",
                      [{"page_number": 1, "markdown": "old", "confidence": 50.0, "needs_review": False}],
                      total_pages=1)
    db.insert_chunks(conn, "book.pdf", "pdf", "Notes",
                      [{"page_number": 1, "markdown": "new", "confidence": 90.0, "needs_review": False}],
                      total_pages=1)
    book_id = db.list_books(conn)[0]["id"]

    deleted = db.delete_book(conn, book_id)

    assert deleted == 2  # both the stale and current row for that page
    assert db.list_documents(conn) == []
