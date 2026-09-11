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
