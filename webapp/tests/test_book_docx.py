import io
import os
import time

from docx import Document
from fastapi.testclient import TestClient
from PIL import Image

from backend import book_docx, convert, db, main as main_module


def _client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.sqlite3")
    real_get_connection = db.get_connection  # capture before patching to avoid self-reference
    monkeypatch.setattr(main_module.db, "get_connection", lambda: real_get_connection(db_path))
    return TestClient(main_module.app), db_path


def _chunk(page, markdown, total=2):
    return {"source_filename": "talks_2568.pdf", "category": "Dhamma", "total_pages": total,
            "page_number": page, "markdown": markdown}


def test_markdown_becomes_headings_paragraphs_lists_and_images(tmp_path):
    os.makedirs(convert.IMAGES_DIR, exist_ok=True)
    Image.new("RGB", (3000, 1500), "white").save(os.path.join(convert.IMAGES_DIR, "abc.jpg"))
    chunks = [
        _chunk(1, "# บทที่ ๑\n\nสติปัฏฐาน **สี่** คือ *ฐาน* ของสติ\nบรรทัดที่สอง\n\n- กาย\n- เวทนา"),
        _chunk(2, "![Photo](/images/abc.jpg)\n\n![Handwriting](/images/missing.jpg)"),
    ]
    document = Document(io.BytesIO(book_docx.book_to_docx(chunks)))
    paragraphs = [(p.style.name, p.text) for p in document.paragraphs if p.text]

    assert paragraphs[0] == ("Title", "talks_2568")
    assert ("Heading 1", "Page 1") in paragraphs and ("Heading 1", "Page 2") in paragraphs
    assert ("Heading 2", "บทที่ ๑") in paragraphs  # page heading is level 1, so "#" becomes level 2
    body = next(p for p in document.paragraphs if p.text.startswith("สติปัฏฐาน"))
    assert body.text == "สติปัฏฐาน สี่ คือ ฐาน ของสติ\nบรรทัดที่สอง"  # line break inside the paragraph kept
    assert [r.text for r in body.runs if r.bold] == ["สี่"] and [r.text for r in body.runs if r.italic] == ["ฐาน"]
    assert [t for s, t in paragraphs if s == "List Bullet"] == ["กาย", "เวทนา"]
    assert len(document.inline_shapes) == 1
    assert document.inline_shapes[0].width <= book_docx.MAX_IMAGE_WIDTH  # wide image scaled to fit the page
    assert "[Handwriting - file missing]" in [t for _, t in paragraphs]


def test_export_needs_complete_reviewed_and_ready_book(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    resp = client.post("/api/upload", files={"file": ("notes.txt", "หน้าแรก\n\nบรรทัด".encode(), "text/plain")})
    job = resp.json()["job_id"]
    for _ in range(100):
        poll = client.get(f"/api/upload/{job}").json()
        if poll["status"] != "processing":
            break
        time.sleep(0.05)
    book = client.get("/api/books").json()[0]
    chunk_id = poll["chunks"][0]["id"]

    not_ready = client.get(f"/api/books/{book['id']}/export/docx")
    assert not_ready.status_code == 409 and "ready for publish" in not_ready.json()["detail"]

    client.post(f"/api/books/{book['id']}/ready-for-publish", json={"ready": True})
    client.put(f"/api/documents/{chunk_id}", json={"markdown": "หน้าแรก\n\nบรรทัด", "needs_review": True})
    flagged = client.get(f"/api/books/{book['id']}/export/docx")
    assert flagged.status_code == 409 and "need review" in flagged.json()["detail"]

    client.put(f"/api/documents/{chunk_id}", json={"markdown": "หน้าแรก\n\nบรรทัด", "needs_review": False})
    ok = client.get(f"/api/books/{book['id']}/export/docx")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert 'filename="notes.docx"' in ok.headers["content-disposition"]
    assert "หน้าแรก" in [p.text for p in Document(io.BytesIO(ok.content)).paragraphs]

    assert client.get("/api/books/9999/export/docx").status_code == 404


def test_export_refuses_a_book_with_pages_still_missing(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    conn = db.get_connection()  # patched by _client to the test database
    db.insert_chunks(conn, "half.pdf", "pdf", "Dhamma",
                     [{"page_number": 1, "markdown": "หน้าหนึ่ง", "confidence": 100, "needs_review": False}],
                     total_pages=3)
    book_id = db.list_books(conn)[0]["id"]
    db.set_ready_for_publish(conn, "half.pdf", 3, True)
    conn.close()
    resp = client.get(f"/api/books/{book_id}/export/docx")
    assert resp.status_code == 409 and "2 page(s) haven't been converted" in resp.json()["detail"]


def test_draft_export_works_at_any_stage_and_is_marked_draft(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    conn = db.get_connection()  # patched by _client to the test database
    db.insert_chunks(conn, "half.pdf", "pdf", "Dhamma", [
        {"page_number": 1, "markdown": "หน้าหนึ่ง", "confidence": 100, "needs_review": False},
        {"page_number": 3, "markdown": "หน้าสาม", "confidence": 60, "needs_review": True},
    ], total_pages=4)
    book_id = db.list_books(conn)[0]["id"]
    conn.close()

    # Not ready, a page flagged, pages missing - the finished export refuses...
    assert client.get(f"/api/books/{book_id}/export/docx").status_code == 409
    # ...the draft doesn't.
    draft = client.get(f"/api/books/{book_id}/export/docx-draft")
    assert draft.status_code == 200
    assert 'filename="half_draft.docx"' in draft.headers["content-disposition"]
    texts = [p.text for p in Document(io.BytesIO(draft.content)).paragraphs]
    assert any(t.startswith("DRAFT - not yet approved") for t in texts)
    assert 'Pages still flagged "Needs review": 3' in texts
    assert "Pages not converted yet (not in this file): 2, 4" in texts
    assert "Page 3 (needs review)" in texts and "Page 1" in texts
    assert "หน้าสาม" in texts

    assert client.get("/api/books/9999/export/docx-draft").status_code == 404


def test_finished_export_has_no_draft_marking():
    chunks = [_chunk(1, "หน้าแรก", total=1)]
    texts = [p.text for p in Document(io.BytesIO(book_docx.book_to_docx(chunks))).paragraphs]
    assert not any("DRAFT" in t for t in texts)
    assert "Page 1" in texts
