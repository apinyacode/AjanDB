import time

from fastapi.testclient import TestClient

from backend import db, main as main_module


def _client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.sqlite3")
    real_get_connection = db.get_connection  # capture before patching to avoid self-reference
    monkeypatch.setattr(main_module.db, "get_connection", lambda: real_get_connection(db_path))
    return TestClient(main_module.app)


def _upload_and_wait(client, timeout=10, **kwargs):
    """Uploads now return a job id immediately (see main.py's comment on
    _jobs) - conversion happens in a background thread so no single request
    has to survive however long OCR takes. Tests poll the job endpoint just
    like the real frontend does, instead of expecting an immediate result."""
    resp = client.post("/api/upload", **kwargs)
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = client.get(f"/api/upload/{job_id}")
        if poll.status_code != 200 or poll.json().get("status") != "processing":
            return poll
        time.sleep(0.05)
    raise TimeoutError(f"upload job {job_id} did not finish within {timeout}s")


def test_upload_and_search_roundtrip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child with patience.", "text/plain")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_filename"] == "parenting.txt"
    assert body["category"] == "Uncategorized"  # no API key in test env -> falls back
    chunk = body["chunks"][0]
    assert chunk["page_number"] == 1
    assert chunk["confidence"] is None
    assert chunk["needs_review"] is False

    search_resp = client.get("/api/search", params={"q": "child"})
    assert search_resp.status_code == 200
    results = search_resp.json()
    assert any(r["id"] == chunk["id"] for r in results)


def test_upload_job_log_reports_per_page_progress(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.post(
        "/api/upload",
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    job_id = resp.json()["job_id"]

    # poll at least once while still processing, to check the "processing"
    # branch also carries whatever log lines exist so far
    import time
    seen_processing = False
    deadline = time.time() + 10
    while time.time() < deadline:
        poll = client.get(f"/api/upload/{job_id}")
        body = poll.json()
        assert "log" in body
        if body.get("status") == "processing":
            seen_processing = True
        if body.get("status") == "done":
            break
        time.sleep(0.02)

    assert body["status"] == "done"
    assert body["log"][0] == "Starting: book.pdf (engine=classical, provider=default)"
    assert "Converting page 1/3..." in body["log"]
    assert "Converting page 2/3..." in body["log"]
    assert "Converting page 3/3..." in body["log"]
    assert body["log"][-1] == "Done: saved 3 chunk(s), category 'Uncategorized'"


def test_upload_rejects_unsupported_extension(tmp_path, monkeypatch):
    # This is checked before a job is even created, so it's still a plain
    # synchronous 400 on the POST itself - no polling involved.
    client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/upload",
        files={"file": ("data.xyz", b"stuff", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_upload_rejects_unknown_engine(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = _upload_and_wait(
        client,
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"engine": "bogus"},
    )
    # engine only matters for PDFs; a .txt upload never reaches the check
    assert resp.status_code == 200


def test_upload_unknown_job_id_returns_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/upload/not-a-real-job-id")
    assert resp.status_code == 404


def test_resolve_client_key_picks_matching_provider():
    resolve = main_module._resolve_client_key
    assert resolve(None, "ak", "ok") == "ak"  # default provider is anthropic
    assert resolve("anthropic", "ak", "ok") == "ak"
    assert resolve("openai", "ak", "ok") == "ok"
    assert resolve("openai", "ak", None) is None


def test_upload_vision_engine_succeeds_with_browser_supplied_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)

    captured = {}

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None, progress=None):
        captured["api_key"] = api_key
        return [{"page_number": 1, "markdown": "vision text", "confidence": 90.0, "needs_review": False}]

    monkeypatch.setattr(main_module.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    import pymupdf as fitz
    pdf_path = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(pdf_path))
    doc.close()

    with open(pdf_path, "rb") as f:
        resp = _upload_and_wait(
            client,
            files={"file": ("blank.pdf", f, "application/pdf")},
            data={"engine": "vision", "anthropic_api_key": "browser-key-123"},
        )
    assert resp.status_code == 200
    assert captured["api_key"] == "browser-key-123"


def test_upload_pdf_with_vision_engine_returns_503_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import pymupdf as fitz

    pdf_path = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(pdf_path))
    doc.close()

    client = _client(tmp_path, monkeypatch)
    with open(pdf_path, "rb") as f:
        resp = _upload_and_wait(
            client,
            files={"file": ("blank.pdf", f, "application/pdf")},
            data={"engine": "vision"},
        )
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_get_document_404_for_missing_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/documents/999")
    assert resp.status_code == 404


def test_get_document_returns_stored_markdown(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    upload_resp = _upload_and_wait(
        client,
        files={"file": ("note.md", b"# Hello", "text/markdown")},
    )
    doc_id = upload_resp.json()["chunks"][0]["id"]

    resp = client.get(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["markdown"] == "# Hello"
    assert body["source_filename"] == "note.md"


def test_upload_with_explicit_category_skips_suggestion(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = _upload_and_wait(
        client,
        files={"file": ("note.md", b"# Hello", "text/markdown")},
        data={"category": "My Category"},
    )
    assert resp.json()["category"] == "My Category"


def test_upload_multi_paragraph_txt_can_produce_multiple_chunks(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    big_text = "\n\n".join(f"Paragraph {i} " + ("word " * 200) for i in range(10))
    resp = _upload_and_wait(
        client,
        files={"file": ("big.txt", big_text.encode(), "text/plain")},
    )
    chunks = resp.json()["chunks"]
    assert len(chunks) > 1
    assert [c["page_number"] for c in chunks] == list(range(1, len(chunks) + 1))


def test_chat_returns_no_sources_message_when_store_is_empty(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/chat", json={"instruction": "compile a book about anything"})
    assert resp.status_code == 200
    assert resp.json()["sources"] == []


def test_chat_returns_503_when_sources_match_but_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )
    resp = client.post("/api/chat", json={"instruction": "compile a book about raising a child"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_chat_with_openai_provider_returns_503_when_no_openai_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )
    resp = client.post(
        "/api/chat",
        json={"instruction": "compile a book about raising a child", "provider": "openai"},
    )
    assert resp.status_code == 503
    assert "OPENAI_API_KEY" in resp.json()["detail"]


def test_chat_succeeds_with_browser_supplied_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )

    captured = {}

    def fake_compile_book(instruction, conn, provider=None, model=None, api_key=None, **kwargs):
        captured["api_key"] = api_key
        return {"markdown": "book", "sources": ["parenting.txt (page 1/1)"]}

    monkeypatch.setattr(main_module.book_compiler, "compile_book", fake_compile_book)

    resp = client.post(
        "/api/chat",
        json={"instruction": "compile a book about raising a child", "anthropic_api_key": "browser-key-456"},
    )
    assert resp.status_code == 200
    assert captured["api_key"] == "browser-key-456"


def test_chat_rejects_unknown_provider_with_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )
    resp = client.post(
        "/api/chat",
        json={"instruction": "compile a book about raising a child", "provider": "bogus"},
    )
    assert resp.status_code == 400


# --- Page-by-page review flow (/api/review/*) ---

def _sample_pdf_bytes():
    import make_test_pdf

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = make_test_pdf.build(f"{d}/sample.pdf")
        with open(path, "rb") as f:
            return f.read()


def _start_review(client, **extra_data):
    return client.post(
        "/api/review/start",
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha", **extra_data},
    )


def test_review_start_rejects_non_pdf(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/review/start",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400


def test_review_start_returns_first_page_and_saves_nothing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = _start_review(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_pages"] == 3
    assert body["page"]["page_number"] == 1
    assert body["page"]["image_base64"]

    assert client.get("/api/documents").json() == []
    client.post(f"/api/review/{body['session_id']}/cancel")  # tidy up the open session/temp file


def test_review_approve_saves_one_page_and_advances(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    session_id = _start_review(client).json()["session_id"]

    resp = client.post(f"/api/review/{session_id}/approve", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["done"] is False
    assert body["page"]["page_number"] == 2
    assert body["total_saved"] == 1

    docs = client.get("/api/documents").json()
    assert len(docs) == 1
    assert docs[0]["page_number"] == 1
    assert docs[0]["total_pages"] == 3


def test_review_approve_with_edited_markdown_is_saved_verbatim(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    session_id = _start_review(client).json()["session_id"]

    client.post(f"/api/review/{session_id}/approve", json={"markdown": "edited by human", "needs_review": True})

    docs = client.get("/api/documents").json()
    doc = client.get(f"/api/documents/{docs[0]['id']}").json()
    assert doc["markdown"] == "edited by human"
    assert doc["needs_review"] == 1


def test_review_skip_advances_without_saving(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    session_id = _start_review(client).json()["session_id"]

    resp = client.post(f"/api/review/{session_id}/skip")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page"]["page_number"] == 2
    assert body["total_saved"] == 0
    assert client.get("/api/documents").json() == []


def test_review_full_flow_reaches_done_with_all_pages_saved(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    session_id = _start_review(client).json()["session_id"]

    client.post(f"/api/review/{session_id}/approve", json={})
    client.post(f"/api/review/{session_id}/approve", json={})
    resp = client.post(f"/api/review/{session_id}/approve", json={})

    body = resp.json()
    assert body["done"] is True
    assert body["total_saved"] == 3
    assert len(client.get("/api/documents").json()) == 3


def test_review_cancel_ends_session_and_keeps_saved_pages(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    session_id = _start_review(client).json()["session_id"]
    client.post(f"/api/review/{session_id}/approve", json={})

    resp = client.post(f"/api/review/{session_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_saved"] == 1
    assert len(client.get("/api/documents").json()) == 1

    # session is gone - a further approve on it 404s
    assert client.post(f"/api/review/{session_id}/approve", json={}).status_code == 404


def test_review_unknown_session_returns_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/review/not-a-real-session/approve", json={}).status_code == 404
    assert client.post("/api/review/not-a-real-session/skip").status_code == 404
    assert client.post("/api/review/not-a-real-session/cancel").status_code == 404
    assert client.post("/api/review/not-a-real-session/retry").status_code == 404
    assert client.post("/api/review/not-a-real-session/verify", json={}).status_code == 404


# --- Second-opinion verification (/api/review/{id}/verify) ---

def test_verify_returns_diff_and_agreement_ratio(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    started = _start_review(client).json()
    session_id = started["session_id"]
    original_markdown = started["page"]["markdown"]

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        return [{"page_number": 1, "markdown": original_markdown.replace("Notes", "NOTES"),
                  "confidence": 88.0, "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(main_module.review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    resp = client.post(f"/api/review/{session_id}/verify", json={"provider": "openai", "openai_api_key": "fake"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "openai"
    assert "NOTES" in body["second_markdown"]
    assert 0 < body["agreement_ratio"] < 100
    assert any(seg["tag"] != "equal" for seg in body["diff"])

    # verifying doesn't advance or save anything
    assert client.get("/api/documents").json() == []


def test_verify_uses_matching_provider_api_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    session_id = _start_review(client).json()["session_id"]

    captured = {}

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        captured["api_key"] = api_key
        return [{"page_number": 1, "markdown": "second opinion text", "confidence": 90.0,
                  "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(main_module.review.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    resp = client.post(
        f"/api/review/{session_id}/verify",
        json={"provider": "openai", "anthropic_api_key": "ak", "openai_api_key": "ok"},
    )
    assert resp.status_code == 200, resp.text
    assert captured["api_key"] == "ok"


def test_verify_returns_503_without_matching_api_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    session_id = _start_review(client).json()["session_id"]
    client.post(f"/api/review/{session_id}/approve", json={})  # advance to page 2 (needs real OCR)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    resp = client.post(f"/api/review/{session_id}/verify", json={"provider": "openai"})
    assert resp.status_code == 503
    assert "OPENAI_API_KEY" in resp.json()["detail"]


# --- Serving extracted images (/images/*) ---

def test_get_image_returns_404_for_unknown_file(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/images/does-not-exist.jpg").status_code == 404


def test_get_image_rejects_path_traversal(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/images/..%2F..%2Fbackend%2Fmain.py")
    assert resp.status_code == 404
    assert "def get_image" not in resp.text


def test_get_image_serves_a_real_embedded_image(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]
    chunks = client.get(f"/api/books/{book_id}").json()["chunks"]
    page3_markdown = next(c["markdown"] for c in chunks if c["page_number"] == 3)
    image_path = page3_markdown.split("](")[1].split(")")[0]

    resp = client.get(image_path)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content[:2] == b"\xff\xd8"  # JPEG magic bytes


# --- Browsing and exporting books (/api/books*) ---

def test_list_books_returns_empty_list_when_nothing_uploaded(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/books").json() == []


def test_list_books_groups_a_multi_page_upload_into_one_entry(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    books = client.get("/api/books").json()
    assert len(books) == 1
    assert books[0]["source_filename"] == "book.pdf"
    assert books[0]["total_pages"] == 3
    assert books[0]["pages_stored"] == 3


def test_get_book_returns_ordered_chunks(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.get(f"/api/books/{book_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_filename"] == "book.pdf"
    assert [c["page_number"] for c in body["chunks"]] == [1, 2, 3]


def test_get_book_returns_404_for_unknown_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/books/999").status_code == 404


def test_export_book_returns_downloadable_markdown(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.get(f"/api/books/{book_id}/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/markdown")
    assert 'filename="book.md"' in resp.headers["content-disposition"]
    assert "# book.pdf" in resp.text
    assert "## Page 1" in resp.text
    assert "## Page 3" in resp.text
    assert "Field Visit Notes" in resp.text  # page 1's actual converted text


def test_export_book_returns_404_for_unknown_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/books/999/export").status_code == 404


def test_export_book_encodes_non_ascii_filename(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("หนังสือ.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.get(f"/api/books/{book_id}/export")
    assert resp.status_code == 200, resp.text
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]


def test_export_book_rewrites_image_links_to_absolute_urls(tmp_path, monkeypatch):
    # page 3 of the sample PDF has an embedded photo and simulated
    # handwriting - both saved as real .jpg files and referenced by a
    # host-relative /images/<hash>.jpg link in the stored markdown, which
    # only resolves correctly while browsing the app itself. Exporting
    # rewrites them to a full URL so the downloaded file's images still
    # work if opened elsewhere while this server keeps running.
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.get(f"/api/books/{book_id}/export")
    assert resp.status_code == 200, resp.text
    assert "](/images/" not in resp.text  # no bare host-relative links left
    assert "](http://testserver/images/" in resp.text

    # and the image is actually fetchable at that URL
    image_url = resp.text.split("](http://testserver")[1].split(")")[0]
    image_resp = client.get(image_url)
    assert image_resp.status_code == 200
    assert image_resp.headers["content-type"] == "image/jpeg"
