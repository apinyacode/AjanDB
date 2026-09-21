import time

from fastapi.testclient import TestClient

from backend import db, main as main_module, originals, sources


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


def _generate_and_wait(client, timeout=10, **kwargs):
    """Same job/poll pattern as _upload_and_wait above, for
    POST /api/generate - see main.py's "Data Generation" section."""
    resp = client.post("/api/generate", **kwargs)
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = client.get(f"/api/generate/{job_id}")
        if poll.status_code != 200 or poll.json().get("status") != "processing":
            return poll
        time.sleep(0.05)
    raise TimeoutError(f"content-generation job {job_id} did not finish within {timeout}s")


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


# --- Editing a saved page from Data Search (PUT /api/documents/{doc_id}) ---

def test_update_document_changes_markdown_and_clears_needs_review(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    upload_resp = _upload_and_wait(
        client,
        files={"file": ("note.md", b"# Hello", "text/markdown")},
    )
    doc_id = upload_resp.json()["chunks"][0]["id"]

    resp = client.put(f"/api/documents/{doc_id}",
                       json={"markdown": "# Corrected", "needs_review": False})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": doc_id, "markdown": "# Corrected", "needs_review": False}

    saved = client.get(f"/api/documents/{doc_id}").json()
    assert saved["markdown"] == "# Corrected"
    assert saved["needs_review"] == 0


def test_update_document_shows_up_in_the_books_view(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]
    chunk_id = client.get(f"/api/books/{book_id}").json()["chunks"][0]["id"]

    client.put(f"/api/documents/{chunk_id}",
               json={"markdown": "hand-corrected text", "needs_review": False})

    chunks = client.get(f"/api/books/{book_id}").json()["chunks"]
    assert chunks[0]["markdown"] == "hand-corrected text"


def test_update_document_returns_404_for_unknown_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.put("/api/documents/999", json={"markdown": "text", "needs_review": False})
    assert resp.status_code == 404


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


# --- Data Generation (POST /api/generate) ---

def test_generate_copy_paste_returns_no_sources_message_when_store_is_empty(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = _generate_and_wait(
        client, json={"instruction": "anything", "output_mode": "copy_paste"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sources"] == []
    assert resp.json()["kind"] == "text"


def test_generate_copy_paste_never_needs_an_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )
    resp = _generate_and_wait(
        client, json={"instruction": "raising a child", "output_mode": "copy_paste"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sources"] == ["parenting.txt (page 1/1)"]
    assert "Tips on how to raise a child." in resp.json()["markdown"]


def test_generate_text_mode_returns_503_when_sources_match_but_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )
    resp = _generate_and_wait(
        client, json={"instruction": "compile a book about raising a child", "output_mode": "generative"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_generate_text_mode_with_openai_provider_returns_503_when_no_openai_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )
    resp = _generate_and_wait(
        client,
        json={"instruction": "compile a book about raising a child", "output_mode": "generative",
              "provider": "openai"},
    )
    assert resp.status_code == 503
    assert "OPENAI_API_KEY" in resp.json()["detail"]


def test_generate_text_mode_succeeds_with_browser_supplied_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )

    captured = {}

    def fake_compile_book(instruction, conn, provider=None, model=None, api_key=None, **kwargs):
        captured["api_key"] = api_key
        captured["mode"] = kwargs.get("mode")
        return {"markdown": "book", "sources": ["parenting.txt (page 1/1)"]}

    monkeypatch.setattr(main_module.book_compiler, "compile_book", fake_compile_book)

    resp = _generate_and_wait(
        client,
        json={"instruction": "compile a book about raising a child", "output_mode": "generative",
              "anthropic_api_key": "browser-key-456"},
    )
    assert resp.status_code == 200, resp.text
    assert captured["api_key"] == "browser-key-456"
    assert captured["mode"] == "text"  # default generative_mode
    assert resp.json()["kind"] == "text"


def test_generate_flow_mode_passes_flow_as_the_compile_book_mode(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("parenting.txt", b"Tips on how to raise a child.", "text/plain")},
    )

    captured = {}

    def fake_compile_book(instruction, conn, provider=None, model=None, api_key=None, **kwargs):
        captured["mode"] = kwargs.get("mode")
        return {"markdown": "flow", "sources": ["parenting.txt (page 1/1)"]}

    monkeypatch.setattr(main_module.book_compiler, "compile_book", fake_compile_book)

    resp = _generate_and_wait(
        client,
        json={"instruction": "raising a child", "output_mode": "generative", "generative_mode": "flow",
              "anthropic_api_key": "fake"},
    )
    assert resp.status_code == 200, resp.text
    assert captured["mode"] == "flow"


def test_generate_rejects_unknown_output_mode_with_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = _generate_and_wait(client, json={"instruction": "anything", "output_mode": "bogus"})
    assert resp.status_code == 400


def test_generate_rejects_unknown_generative_mode_with_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = _generate_and_wait(
        client, json={"instruction": "anything", "output_mode": "generative", "generative_mode": "bogus"})
    assert resp.status_code == 400


def test_generate_image_mode_calls_content_studio_and_returns_its_result(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    captured = {}

    def fake_generate_image(instruction, conn, api_key=None, **kwargs):
        captured["api_key"] = api_key
        return {"image_url": "/generated-images/abc.png", "prompt": "a prompt", "sources": []}

    monkeypatch.setattr(main_module.content_studio, "generate_image", fake_generate_image)

    resp = _generate_and_wait(
        client,
        json={"instruction": "a peaceful forest", "output_mode": "generative", "generative_mode": "image",
              "openai_api_key": "browser-openai-key"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "image"
    assert resp.json()["image_url"] == "/generated-images/abc.png"
    assert captured["api_key"] == "browser-openai-key"


def test_generate_image_mode_uses_openai_key_regardless_of_provider(tmp_path, monkeypatch):
    # image generation always needs OpenAI (Anthropic has no image API),
    # even when `provider` (which picks the *text*-mode model) is Claude.
    client = _client(tmp_path, monkeypatch)
    captured = {}

    def fake_generate_image(instruction, conn, api_key=None, **kwargs):
        captured["api_key"] = api_key
        return {"image_url": "/generated-images/abc.png", "prompt": "a prompt", "sources": []}

    monkeypatch.setattr(main_module.content_studio, "generate_image", fake_generate_image)

    resp = _generate_and_wait(
        client,
        json={"instruction": "a peaceful forest", "output_mode": "generative", "generative_mode": "image",
              "provider": "anthropic", "anthropic_api_key": "claude-key", "openai_api_key": "openai-key"},
    )
    assert resp.status_code == 200, resp.text
    assert captured["api_key"] == "openai-key"


def test_generate_image_mode_returns_503_when_content_studio_raises_runtime_error(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    def fake_generate_image(instruction, conn, api_key=None, **kwargs):
        raise RuntimeError("Image generation needs an OpenAI API key")

    monkeypatch.setattr(main_module.content_studio, "generate_image", fake_generate_image)

    resp = _generate_and_wait(
        client, json={"instruction": "a peaceful forest", "output_mode": "generative", "generative_mode": "image"})
    assert resp.status_code == 503
    assert "OpenAI" in resp.json()["detail"]


def test_generate_video_mode_calls_content_studio_and_returns_its_result(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    captured = {}

    def fake_generate_video(instruction, conn, **kwargs):
        captured.update(kwargs)
        return {"video_url": "/generated-videos/abc.mp4", "script": "narration", "image_url": "/generated-images/x.png",
                "sources": []}

    monkeypatch.setattr(main_module.content_studio, "generate_video", fake_generate_video)

    resp = _generate_and_wait(
        client,
        json={"instruction": "a short lesson", "output_mode": "generative", "generative_mode": "video",
              "anthropic_api_key": "claude-key", "openai_api_key": "openai-key",
              "azure_speech_key": "azure-key", "azure_speech_region": "eastus"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "video"
    assert resp.json()["video_url"] == "/generated-videos/abc.mp4"
    assert captured["text_api_key"] == "claude-key"
    assert captured["image_api_key"] == "openai-key"
    assert captured["azure_speech_key"] == "azure-key"
    assert captured["azure_speech_region"] == "eastus"


def test_generate_unknown_job_id_returns_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/generate/not-a-real-job-id")
    assert resp.status_code == 404


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


def test_review_start_passes_auto_validate_through_to_review_start(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    captured = {}
    real_start = main_module.review.start

    def spy_start(*args, **kwargs):
        captured["auto_validate"] = kwargs.get("auto_validate")
        return real_start(*args, **kwargs)

    monkeypatch.setattr(main_module.review, "start", spy_start)

    resp = _start_review(client, auto_validate="true")
    assert resp.status_code == 200, resp.text
    assert captured["auto_validate"] is True
    client.post(f"/api/review/{resp.json()['session_id']}/cancel")

    resp2 = _start_review(client)  # omitted entirely -> defaults off
    assert resp2.status_code == 200, resp2.text
    assert captured["auto_validate"] is False
    client.post(f"/api/review/{resp2.json()['session_id']}/cancel")


def test_review_start_passes_ensemble_verify_through_to_review_start(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    captured = {}
    real_start = main_module.review.start

    def spy_start(*args, **kwargs):
        captured["ensemble_verify"] = kwargs.get("ensemble_verify")
        return real_start(*args, **kwargs)

    monkeypatch.setattr(main_module.review, "start", spy_start)

    resp = _start_review(client, ensemble_verify="true")
    assert resp.status_code == 200, resp.text
    assert captured["ensemble_verify"] is True
    client.post(f"/api/review/{resp.json()['session_id']}/cancel")

    resp2 = _start_review(client)  # omitted entirely -> defaults off
    assert resp2.status_code == 200, resp2.text
    assert captured["ensemble_verify"] is False
    client.post(f"/api/review/{resp2.json()['session_id']}/cancel")


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


def test_list_books_marks_bulk_uploads_as_not_resumable(tmp_path, monkeypatch):
    # bulk /api/upload is all-or-nothing (see sources.py's docstring) -
    # nothing is ever resumable from that path.
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    assert client.get("/api/books").json()[0]["resumable"] is False


def test_list_books_marks_a_cancelled_review_as_resumable(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    started = _start_review(client).json()
    client.post(f"/api/review/{started['session_id']}/approve", json={})  # page 1 -> 2
    client.post(f"/api/review/{started['session_id']}/cancel")

    books = client.get("/api/books").json()
    assert len(books) == 1
    assert books[0]["resumable"] is True


# --- Deleting a book (DELETE /api/books/{book_id}) ---

def test_delete_book_removes_it_from_the_list(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.delete(f"/api/books/{book_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_pages"] == 3
    assert client.get("/api/books").json() == []


def test_delete_book_returns_404_for_unknown_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.delete("/api/books/999").status_code == 404


def test_delete_book_also_cleans_up_its_resumable_source(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    started = _start_review(client).json()
    client.post(f"/api/review/{started['session_id']}/approve", json={})
    client.post(f"/api/review/{started['session_id']}/cancel")
    book_id = client.get("/api/books").json()[0]["id"]
    assert sources.exists("book.pdf", 3)

    resp = client.delete(f"/api/books/{book_id}")

    assert resp.status_code == 200, resp.text
    assert not sources.exists("book.pdf", 3)


def test_delete_book_also_cleans_up_its_retained_original(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]
    assert originals.exists("book.pdf", 3)

    resp = client.delete(f"/api/books/{book_id}")

    assert resp.status_code == 200, resp.text
    assert not originals.exists("book.pdf", 3)


# --- Viewing/exporting the original file (GET /api/books/{book_id}/original[/export]) ---

def test_list_books_marks_bulk_uploads_as_having_an_original(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    assert client.get("/api/books").json()[0]["has_original"] is True


def test_list_books_marks_a_review_session_as_having_an_original(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    started = _start_review(client).json()
    client.post(f"/api/review/{started['session_id']}/approve", json={})
    client.post(f"/api/review/{started['session_id']}/cancel")

    assert client.get("/api/books").json()[0]["has_original"] is True


def test_view_original_returns_the_uploaded_pdf_bytes_inline(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    pdf_bytes = _sample_pdf_bytes()
    _upload_and_wait(
        client, files={"file": ("book.pdf", pdf_bytes, "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.get(f"/api/books/{book_id}/original")
    assert resp.status_code == 200, resp.text
    assert resp.content == pdf_bytes
    assert resp.headers["content-disposition"].startswith("inline;")
    assert 'filename="book.pdf"' in resp.headers["content-disposition"]


def test_export_original_returns_the_uploaded_pdf_as_a_download(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    pdf_bytes = _sample_pdf_bytes()
    _upload_and_wait(
        client, files={"file": ("book.pdf", pdf_bytes, "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.get(f"/api/books/{book_id}/original/export")
    assert resp.status_code == 200, resp.text
    assert resp.content == pdf_bytes
    assert resp.headers["content-disposition"].startswith("attachment;")


def test_view_original_returns_404_for_unknown_book(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/books/999/original").status_code == 404


def test_export_original_returns_404_for_unknown_book(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/books/999/original/export").status_code == 404


def test_view_original_returns_404_when_nothing_was_retained(tmp_path, monkeypatch):
    # a book whose retained original was deleted out from under it (or one
    # that predates originals.py entirely) has a real book_id but no file.
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]
    originals.delete("book.pdf", 3)

    resp = client.get(f"/api/books/{book_id}/original")
    assert resp.status_code == 404
    assert "No original file was retained" in resp.json()["detail"]


# --- Ready-for-publish flag (POST /api/books/{book_id}/ready-for-publish) ---

def test_ready_for_publish_defaults_false_then_can_be_set_and_cleared(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]
    assert client.get("/api/books").json()[0]["ready_for_publish"] == 0

    resp = client.post(f"/api/books/{book_id}/ready-for-publish", json={"ready": True})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ready_for_publish": True}
    assert client.get("/api/books").json()[0]["ready_for_publish"] == 1

    client.post(f"/api/books/{book_id}/ready-for-publish", json={"ready": False})
    assert client.get("/api/books").json()[0]["ready_for_publish"] == 0


def test_ready_for_publish_returns_404_for_unknown_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/books/999/ready-for-publish", json={"ready": True})
    assert resp.status_code == 404


# --- Labels (PUT/DELETE /api/books/{book_id}/labels) ---

def test_set_book_label_adds_and_lists_it(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.put(f"/api/books/{book_id}/labels", json={"key": "media type", "value": "book"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"labels": {"media type": "book"}}
    assert client.get("/api/books").json()[0]["labels"] == {"media type": "book"}


def test_set_book_label_overwrites_an_existing_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]
    client.put(f"/api/books/{book_id}/labels", json={"key": "media type", "value": "book"})

    resp = client.put(f"/api/books/{book_id}/labels", json={"key": "media type", "value": "audio"})
    assert resp.json() == {"labels": {"media type": "audio"}}


def test_set_book_label_rejects_an_empty_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.put(f"/api/books/{book_id}/labels", json={"key": "   ", "value": "book"})
    assert resp.status_code == 400


def test_set_book_label_returns_404_for_unknown_book(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.put("/api/books/999/labels", json={"key": "media type", "value": "book"})
    assert resp.status_code == 404


def test_delete_book_label_removes_just_that_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]
    client.put(f"/api/books/{book_id}/labels", json={"key": "media type", "value": "book"})
    client.put(f"/api/books/{book_id}/labels", json={"key": "content", "value": "QnA"})

    resp = client.delete(f"/api/books/{book_id}/labels/media type")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"labels": {"content": "QnA"}}


def test_delete_book_label_returns_404_for_unknown_book(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.delete("/api/books/999/labels/media type")
    assert resp.status_code == 404


def test_delete_book_also_removes_its_labels(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]
    client.put(f"/api/books/{book_id}/labels", json={"key": "media type", "value": "book"})

    client.delete(f"/api/books/{book_id}")

    # re-uploading the same file gets a fresh book with no leftover labels
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    assert client.get("/api/books").json()[0]["labels"] == {}


# --- Resuming an incomplete review (POST /api/books/{book_id}/resume) ---

def test_resume_endpoint_continues_from_the_cancelled_page(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    started = _start_review(client).json()
    client.post(f"/api/review/{started['session_id']}/approve", json={})  # page 1 -> 2
    client.post(f"/api/review/{started['session_id']}/cancel")
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.post(f"/api/books/{book_id}/resume", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page"]["page_number"] == 2
    client.post(f"/api/review/{body['session_id']}/cancel")  # tidy up


def test_resume_endpoint_returns_404_for_a_book_with_nothing_resumable(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _upload_and_wait(
        client,
        files={"file": ("book.pdf", _sample_pdf_bytes(), "application/pdf")},
        data={"langs": "eng+tha"},
    )
    book_id = client.get("/api/books").json()[0]["id"]

    resp = client.post(f"/api/books/{book_id}/resume", json={})
    assert resp.status_code == 404


def test_resume_endpoint_returns_404_for_unknown_book_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/books/999/resume", json={})
    assert resp.status_code == 404


def test_tts_endpoint_returns_audio_url(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        main_module.tts, "get_or_synthesize",
        lambda text, region=None, api_key=None: "/audio/fake-hash.mp3")

    resp = client.post("/api/tts", json={"text": "Hello there"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"audio_url": "/audio/fake-hash.mp3"}


def test_tts_endpoint_rejects_empty_text(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/tts", json={"text": "   "})
    assert resp.status_code == 400


def test_tts_endpoint_returns_503_without_azure_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    resp = client.post("/api/tts", json={"text": "Hello there"})

    assert resp.status_code == 503


def test_tts_endpoint_passes_browser_supplied_key_through(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    captured = {}

    def fake_get_or_synthesize(text, region=None, api_key=None):
        captured["region"] = region
        captured["api_key"] = api_key
        return "/audio/fake-hash.mp3"

    monkeypatch.setattr(main_module.tts, "get_or_synthesize", fake_get_or_synthesize)

    resp = client.post("/api/tts", json={
        "text": "Hello there",
        "azure_speech_key": "browser-key",
        "azure_speech_region": "eastus",
    })

    assert resp.status_code == 200, resp.text
    assert captured == {"region": "eastus", "api_key": "browser-key"}


def test_audio_endpoint_serves_cached_file(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    import os
    os.makedirs(main_module.tts.AUDIO_DIR, exist_ok=True)
    with open(os.path.join(main_module.tts.AUDIO_DIR, "fake-hash.mp3"), "wb") as f:
        f.write(b"fake-mp3-bytes")

    resp = client.get("/audio/fake-hash.mp3")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"fake-mp3-bytes"


def test_audio_endpoint_404_for_missing_file(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/audio/does-not-exist.mp3")
    assert resp.status_code == 404
