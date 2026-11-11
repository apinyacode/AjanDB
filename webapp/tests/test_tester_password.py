import base64

from fastapi.testclient import TestClient

from backend import main as main_module


def _auth(password: str, user: str = "tester") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_no_password_configured_leaves_the_app_open():
    client = TestClient(main_module.app)
    assert client.get("/").status_code == 200
    assert client.get("/api/qna/documents").status_code == 200


def test_password_protects_pages_api_and_asks_the_browser(monkeypatch):
    monkeypatch.setenv("AJANDB_PASSWORD", "lotus-2568")
    client = TestClient(main_module.app)

    for path in ("/", "/api/qna/documents", "/api/books", "/app.js"):
        blocked = client.get(path)
        assert blocked.status_code == 401, path
        assert blocked.headers["www-authenticate"].startswith("Basic ")
    assert client.delete("/api/qna/documents/1").status_code == 401  # destructive calls too

    assert client.get("/", headers=_auth("wrong")).status_code == 401
    assert client.get("/", headers={"Authorization": "Basic !!!not-base64"}).status_code == 401
    assert client.get("/", headers=_auth("lotus-2568")).status_code == 200
    # Any username works - testers only need the one shared password.
    assert client.get("/api/qna/documents", headers=_auth("lotus-2568", user="somchai")).status_code == 200


def test_thai_password_works(monkeypatch):
    monkeypatch.setenv("AJANDB_PASSWORD", "ธรรมะ")
    client = TestClient(main_module.app)
    assert client.get("/", headers=_auth("ธรรมะ")).status_code == 200


def test_health_check_answers_without_the_password(monkeypatch):
    monkeypatch.setenv("AJANDB_PASSWORD", "lotus-2568")
    client = TestClient(main_module.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert client.get("/healthz/../api/books").status_code == 401  # no way around the password through it
