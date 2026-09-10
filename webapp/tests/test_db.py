from backend import db


def _tmp_conn(tmp_path):
    return db.get_connection(str(tmp_path / "test.sqlite3"))


def test_insert_and_get_document(tmp_path):
    conn = _tmp_conn(tmp_path)
    doc_id = db.insert_document(conn, "notes.md", "# Hello\nWorld")
    doc = db.get_document(conn, doc_id)
    assert doc["filename"] == "notes.md"
    assert "Hello" in doc["markdown"]


def test_get_document_returns_none_for_missing_id(tmp_path):
    conn = _tmp_conn(tmp_path)
    assert db.get_document(conn, 999) is None


def test_search_finds_matching_document(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_document(conn, "parenting.md", "Tips on how to raise a child with patience.")
    db.insert_document(conn, "cooking.md", "A recipe for pasta.")
    results = db.search(conn, "child")
    assert len(results) == 1
    assert results[0]["filename"] == "parenting.md"
    assert "<mark>" in results[0]["snippet"]


def test_search_returns_empty_list_for_no_matches(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_document(conn, "cooking.md", "A recipe for pasta.")
    assert db.search(conn, "spaceship") == []


def test_search_with_special_characters_does_not_raise(tmp_path):
    conn = _tmp_conn(tmp_path)
    db.insert_document(conn, "notes.md", "Some content here.")
    # FTS5 treats "*, -, (, )" etc. as syntax - the query sanitizer must
    # neutralise that rather than let sqlite3.OperationalError propagate.
    assert db.search(conn, 'weird" query (with) -syntax*') == []


def test_list_documents_orders_newest_first(tmp_path):
    conn = _tmp_conn(tmp_path)
    id1 = db.insert_document(conn, "first.md", "one")
    id2 = db.insert_document(conn, "second.md", "two")
    docs = db.list_documents(conn)
    assert docs[0]["id"] == id2
    assert docs[1]["id"] == id1
