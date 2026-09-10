from fastapi.testclient import TestClient

from backend import db, main as main_module


def _client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.sqlite3")
    real_get_connection = db.get_connection  # capture before patching to avoid self-reference
    monkeypatch.setattr(main_module.db, "get_connection", lambda: real_get_connection(db_path))
    return TestClient(main_module.app)


def test_upload_and_search_roundtrip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/upload",
        files={"file": ("parenting.txt", b"Tips on how to raise a child with patience.", "text/plain")},
    )
    assert resp.status_code == 200
    doc_id = resp.json()["id"]

    search_resp = client.get("/api/search", params={"q": "child"})
    assert search_resp.status_code == 200
    results = search_resp.json()
    assert any(r["id"] == doc_id for r in results)


def test_upload_rejects_unsupported_extension(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/upload",
        files={"file": ("data.xyz", b"stuff", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_get_document_404_for_missing_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/documents/999")
    assert resp.status_code == 404


def test_get_document_returns_stored_markdown(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    upload_resp = client.post(
        "/api/upload",
        files={"file": ("note.md", b"# Hello", "text/markdown")},
    )
    doc_id = upload_resp.json()["id"]

    resp = client.get(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.json()["markdown"] == "# Hello"


def test_chat_returns_no_sources_message_when_store_is_empty(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/chat", json={"instruction": "compile a book about anything"})
    assert resp.status_code == 200
    assert resp.json()["sources"] == []


def test_chat_returns_503_when_sources_match_but_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/api/upload",
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )
    resp = client.post("/api/chat", json={"instruction": "compile a book about raising a child"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]
