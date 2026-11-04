import csv
import io
import os
import time

import pymupdf as fitz
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from backend import db, main as main_module, qna


def _client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.sqlite3")
    real_get_connection = db.get_connection  # capture before patching to avoid self-reference
    monkeypatch.setattr(main_module.db, "get_connection", lambda: real_get_connection(db_path))
    return TestClient(main_module.app)


def _make_pdf(pages: int = 3) -> bytes:
    doc = fitz.open()
    for n in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((150, 200), f"Question slip {n}", fontsize=20)
        page.insert_text((150, 240), "How should I practise?", fontsize=20)
    return doc.tobytes()


def _fake_result(confidence="high", unsure=False):
    return {
        "lines": ["Dear Ajahn,", "How should I ⟦practise?⟧" if unsure else "How should I practise?"],
        "corrections": [{"line": 2, "as_written": "practis", "corrected": "practise"}],
        "greeting": "Dear Ajahn,", "question": "How should I practise?", "closing": "",
        "continues_from_previous": False, "continues_to_next": True,
        "notes": "Underlined word.", "confidence": confidence,
        "_meta": {"model": "fake", "input_tokens": 1, "output_tokens": 1},
    }


def _wait(client, job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = client.get(f"/api/qna/jobs/{job_id}")
        if poll.status_code != 200 or poll.json().get("status") != "processing":
            return poll
        time.sleep(0.05)
    raise TimeoutError(f"QnA job {job_id} did not finish within {timeout}s")


def _upload(client, pages=3):
    resp = client.post("/api/qna/upload", data={"anthropic_api_key": "sk-ant-test"},
                       files={"file": ("slips_2568.pdf", _make_pdf(pages), "application/pdf")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_crop_ignores_scanner_edge_lines():
    page = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(page)
    for x in range(300, 600, 12):                          # "writing": thin, sparse pen strokes
        draw.line((x, 400, x + 8, 500), fill="black", width=2)
    draw.line((970, 0, 970, 1400), fill="black", width=4)  # scanner edge, inside the ignored side band
    draw.line((900, 0, 900, 1400), fill="black", width=4)  # a long line further in: still not writing
    crop = qna.crop_to_handwriting(page)
    # Only the writing (plus padding), enlarged - not stretched to the lines or the full page height.
    assert crop.width > crop.height
    assert abs(crop.width / crop.height - (296 + 2 * qna.CROP_PADDING) / (100 + 2 * qna.CROP_PADDING)) < 0.05


def test_crop_keeps_blank_page_whole():
    page = Image.new("RGB", (800, 1000), "white")
    assert qna.crop_to_handwriting(page).size == (800, 1000)


def test_to_fields_formats_review_fields():
    fields = qna.to_fields(_fake_result(unsure=True))
    assert fields["text"] == "Dear Ajahn,\nHow should I ⟦practise?⟧"
    assert fields["clean"] == "Dear Ajahn, How should I practise?"
    assert fields["fixes"] == "line 2: practis -> practise"
    assert fields["notes"] == "Underlined word. Continues on the next page."
    assert qna.unsure_count(fields["text"]) == 1


def test_upload_transcribes_every_page_and_tracks_status(tmp_path, monkeypatch):
    calls = []

    def fake_claude(full_page, crop, api_key):
        calls.append(api_key)
        if len(calls) == 2:
            raise RuntimeError("no transcription returned (stop_reason=refusal)")
        return _fake_result(confidence="low", unsure=True)

    monkeypatch.setattr(qna, "_call_claude", fake_claude)
    client = _client(tmp_path, monkeypatch)
    started = _upload(client, pages=3)
    done = _wait(client, started["job_id"])
    assert done.status_code == 200, done.text
    assert done.json()["succeeded"] == 2 and done.json()["failed"] == 1
    assert calls == ["sk-ant-test"] * 3  # the browser-supplied key reaches the model call

    docs = client.get("/api/qna/documents").json()
    assert docs == [{**docs[0], "source_filename": "slips_2568.pdf", "total_pages": 3,
                     "pending": 0, "review": 2, "done": 0, "error": 1}]

    pages = client.get(f"/api/qna/documents/{started['document_id']}").json()["pages"]
    assert [p["status"] for p in pages].count("error") == 1
    good = next(p for p in pages if p["status"] == "review")
    assert good["unsure"] == 1 and good["confidence"] == "low" and "ai_json" not in good

    # Both page images exist and are served.
    for kind in ("crop", "full"):
        img = client.get(f"/api/qna/images/{started['document_id']}/{good['page_number']}?kind={kind}")
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"


def test_retry_only_resends_failed_pages(tmp_path, monkeypatch):
    attempts = []

    def flaky_claude(full_page, crop, api_key):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")
        return _fake_result()

    monkeypatch.setattr(qna, "_call_claude", flaky_claude)
    client = _client(tmp_path, monkeypatch)
    started = _upload(client, pages=2)
    _wait(client, started["job_id"])
    assert len(attempts) == 2

    retry = client.post(f"/api/qna/documents/{started['document_id']}/transcribe",
                        json={"anthropic_api_key": "sk-ant-test"})
    result = _wait(client, retry.json()["job_id"]).json()
    assert (result["succeeded"], result["failed"]) == (1, 0)
    assert len(attempts) == 3  # only the one failed page went back to the model
    doc = client.get("/api/qna/documents").json()[0]
    assert (doc["review"], doc["error"]) == (2, 0)


def test_review_edit_mark_done_and_export(tmp_path, monkeypatch):
    monkeypatch.setattr(qna, "_call_claude", lambda full_page, crop, api_key: _fake_result())
    client = _client(tmp_path, monkeypatch)
    started = _upload(client, pages=2)
    _wait(client, started["job_id"])
    document_id = started["document_id"]
    first = client.get(f"/api/qna/documents/{document_id}").json()["pages"][0]

    saved = client.put(f"/api/qna/pages/{first['id']}", json={
        "text": "Dear Ajahn,\nHow should I meditate?", "clean": "Dear Ajahn, How should I meditate?",
        "fixes": "", "notes": "Checked.", "status": "done"})
    assert saved.status_code == 200
    assert saved.json()["status"] == "done" and saved.json()["reviewed_at"]
    assert client.put(f"/api/qna/pages/{first['id']}", json={
        "text": "", "clean": "", "fixes": "", "notes": "", "status": "finished"}).status_code == 400
    assert client.put("/api/qna/pages/9999", json={
        "text": "", "clean": "", "fixes": "", "notes": "", "status": "done"}).status_code == 404

    exported = client.get(f"/api/qna/documents/{document_id}/export?format=csv")
    assert exported.status_code == 200
    assert exported.content.startswith(b"\xef\xbb\xbf")  # BOM, so Excel reads Thai correctly
    assert 'filename="slips_2568_qna.csv"' in exported.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert [(r["page"], r["status"]) for r in rows] == [("1", "done"), ("2", "review")]
    assert rows[0]["clean"] == "Dear Ajahn, How should I meditate?"
    assert rows[1]["spelling_fixes"] == "line 2: practis -> practise"

    done_only = client.get(f"/api/qna/documents/{document_id}/export?format=csv&done_only=true")
    assert len(list(csv.DictReader(io.StringIO(done_only.content.decode("utf-8-sig"))))) == 1

    docx = client.get(f"/api/qna/documents/{document_id}/export?format=docx")
    assert docx.status_code == 200
    text = "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)
    assert "Page 1  (done)" in text and "Dear Ajahn, How should I meditate?" in text
    assert client.get(f"/api/qna/documents/{document_id}/export?format=pdf").status_code == 400


def test_delete_removes_pages_and_images(tmp_path, monkeypatch):
    monkeypatch.setattr(qna, "_call_claude", lambda full_page, crop, api_key: _fake_result())
    client = _client(tmp_path, monkeypatch)
    started = _upload(client, pages=1)
    _wait(client, started["job_id"])
    image_dir = os.path.join(qna.QNA_DIR, str(started["document_id"]))
    assert os.path.isdir(image_dir)

    assert client.delete(f"/api/qna/documents/{started['document_id']}").status_code == 200
    assert client.get("/api/qna/documents").json() == []
    assert not os.path.exists(image_dir)
    assert client.delete(f"/api/qna/documents/{started['document_id']}").status_code == 404


def test_upload_rejects_missing_key_and_non_pdf(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    no_key = client.post("/api/qna/upload", files={"file": ("a.pdf", _make_pdf(1), "application/pdf")})
    assert no_key.status_code == 400 and "Anthropic API key" in no_key.json()["detail"]

    not_pdf = client.post("/api/qna/upload", data={"anthropic_api_key": "sk-ant-test"},
                          files={"file": ("a.txt", b"hello", "text/plain")})
    assert not_pdf.status_code == 400
    assert client.get("/api/qna/documents").json() == []


def test_backend_environment_key_is_enough(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    monkeypatch.setattr(qna, "_call_claude", lambda full_page, crop, api_key: seen.append(api_key) or _fake_result())
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/qna/upload", files={"file": ("a.pdf", _make_pdf(1), "application/pdf")})
    assert resp.status_code == 200
    _wait(client, resp.json()["job_id"])
    assert seen == [None]  # no browser key -> the SDK client reads the environment itself
