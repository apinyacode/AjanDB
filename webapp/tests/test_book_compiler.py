from types import SimpleNamespace

from backend import book_compiler, db


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMessages:
    def __init__(self, response_text):
        self._response_text = response_text

    def create(self, **kwargs):
        return SimpleNamespace(content=[_FakeTextBlock(self._response_text)])


class _FakeAnthropic:
    def __init__(self, response_text, api_key=None):
        self.messages = _FakeMessages(response_text)


class _FakeOpenAIMessage:
    def __init__(self, content):
        self.content = content


class _FakeOpenAIChoice:
    def __init__(self, content):
        self.message = _FakeOpenAIMessage(content)


class _FakeOpenAICompletions:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        return SimpleNamespace(choices=[_FakeOpenAIChoice(self._content)])


class _FakeOpenAIChat:
    def __init__(self, content):
        self.completions = _FakeOpenAICompletions(content)


class _FakeOpenAI:
    def __init__(self, content, api_key=None):
        self.chat = _FakeOpenAIChat(content)


def _tmp_conn(tmp_path):
    return db.get_connection(str(tmp_path / "test.sqlite3"))


def _insert(conn, filename, content, category="Uncategorized"):
    chunk = [{"page_number": 1, "markdown": content, "confidence": None, "needs_review": False}]
    db.insert_chunks(conn, filename, "md", category, chunk)


def test_extract_keywords_drops_instruction_stopwords():
    kw = book_compiler._extract_keywords(
        "compile a book from all the files containing content about how to raise a child"
    )
    assert "child" in kw
    assert "raise" in kw
    assert "compile" not in kw
    assert "book" not in kw


def test_compile_book_returns_no_sources_message_when_nothing_matches(tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "cooking.md", "A recipe for pasta.")
    result = book_compiler.compile_book("compile a book about dinosaurs", conn)
    assert result["sources"] == []
    assert "No matching" in result["markdown"]


def test_compile_book_raises_clear_error_when_no_api_key(monkeypatch, tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    try:
        book_compiler.compile_book("compile a book about raising a child", conn)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "ANTHROPIC_API_KEY" in str(e)


def test_compile_book_raises_clear_error_when_no_openai_key(monkeypatch, tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    try:
        book_compiler.compile_book("compile a book about raising a child", conn, provider="openai")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "OPENAI_API_KEY" in str(e)


def test_compile_book_rejects_unknown_provider(tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")
    try:
        book_compiler.compile_book("compile a book about raising a child", conn, provider="bogus")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "bogus" in str(e)


def test_compile_book_sends_matching_sources_to_model(monkeypatch, tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")
    _insert(conn, "cooking.md", "A recipe for pasta.")

    monkeypatch.setattr(
        book_compiler, "Anthropic",
        lambda api_key=None: _FakeAnthropic("# A Book About Raising a Child\n\n..."),
    )

    result = book_compiler.compile_book(
        "compile a book from all the files containing content about how to raise a child",
        conn, api_key="fake",
    )
    assert result["sources"] == ["parenting.md (page 1/1)"]
    assert "A Book About Raising a Child" in result["markdown"]


def test_strip_embedded_images_replaces_data_uri_with_short_label():
    markdown = "Some text\n\n![handwriting](data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==)\n\nMore text"
    stripped = book_compiler._strip_embedded_images(markdown)
    assert "base64" not in stripped
    assert "Some text" in stripped and "More text" in stripped
    assert "[handwriting]" in stripped


def test_strip_embedded_images_replaces_image_file_link_with_short_label():
    markdown = "Some text\n\n![handwriting](/images/3f9a2b8c1d4e.jpg)\n\nMore text"
    stripped = book_compiler._strip_embedded_images(markdown)
    assert "/images/" not in stripped
    assert "Some text" in stripped and "More text" in stripped
    assert "[handwriting]" in stripped


def test_compile_book_strips_embedded_images_before_sending_to_model(monkeypatch, tmp_path):
    conn = _tmp_conn(tmp_path)
    huge_fake_base64 = "A" * 5000  # stand-in for a real embedded image's bulk
    _insert(conn, "book.pdf",
            f"Field notes about raising a child.\n\n![handwriting](data:image/png;base64,{huge_fake_base64})")

    captured = {}

    class _CapturingAnthropic(_FakeAnthropic):
        def __init__(self, api_key=None):
            super().__init__("# Book", api_key=api_key)

    def _capturing_create(self, **kwargs):
        captured["user_message"] = kwargs["messages"][0]["content"]
        return SimpleNamespace(content=[_FakeTextBlock("# Book")])

    monkeypatch.setattr(_FakeMessages, "create", _capturing_create)
    monkeypatch.setattr(book_compiler, "Anthropic", _CapturingAnthropic)

    book_compiler.compile_book("compile a book about raising a child", conn, api_key="fake")

    assert huge_fake_base64 not in captured["user_message"]
    assert "[handwriting]" in captured["user_message"]
    assert "Field notes about raising a child" in captured["user_message"]


def test_compile_book_uses_openai_when_provider_selected(monkeypatch, tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")
    _insert(conn, "cooking.md", "A recipe for pasta.")

    monkeypatch.setattr(
        book_compiler, "OpenAI",
        lambda api_key=None: _FakeOpenAI("# A Book About Raising a Child (via GPT)\n\n..."),
    )

    result = book_compiler.compile_book(
        "compile a book from all the files containing content about how to raise a child",
        conn, provider="openai", api_key="fake",
    )
    assert result["sources"] == ["parenting.md (page 1/1)"]
    assert "via GPT" in result["markdown"]


def test_compile_book_rejects_unknown_mode(tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")
    try:
        book_compiler.compile_book(
            "compile a book about raising a child", conn, api_key="fake", mode="bogus")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "bogus" in str(e)


def test_compile_book_flow_mode_uses_the_flow_system_prompt(monkeypatch, tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")

    captured = {}

    def _capturing_create(self, **kwargs):
        captured["system"] = kwargs["system"]
        return SimpleNamespace(content=[_FakeTextBlock("# Flow")])

    monkeypatch.setattr(_FakeMessages, "create", _capturing_create)
    monkeypatch.setattr(book_compiler, "Anthropic", lambda api_key=None: _FakeAnthropic("# Flow"))

    book_compiler.compile_book("compile about raising a child", conn, api_key="fake", mode="flow")

    assert captured["system"] == book_compiler._FLOW_SYSTEM_PROMPT
    assert "different" in captured["system"]


def test_compile_book_video_script_mode_uses_the_video_script_system_prompt(monkeypatch, tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")

    captured = {}

    def _capturing_create(self, **kwargs):
        captured["system"] = kwargs["system"]
        return SimpleNamespace(content=[_FakeTextBlock("Some narration.")])

    monkeypatch.setattr(_FakeMessages, "create", _capturing_create)
    monkeypatch.setattr(book_compiler, "Anthropic", lambda api_key=None: _FakeAnthropic("Some narration."))

    result = book_compiler.compile_book(
        "compile about raising a child", conn, api_key="fake", mode="video_script")

    assert captured["system"] == book_compiler._VIDEO_SCRIPT_SYSTEM_PROMPT
    assert result["markdown"] == "Some narration."


def test_retrieve_matches_returns_full_documents_with_labels(tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")

    docs = book_compiler.retrieve_matches("raising a child", conn)

    assert len(docs) == 1
    assert docs[0]["source_filename"] == "parenting.md"
    assert docs[0]["label"] == "parenting.md (page 1/1)"
    assert docs[0]["markdown"] == "Tips on how to raise a child with patience."


def test_compile_copy_paste_returns_verbatim_content_with_citations(tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")
    _insert(conn, "cooking.md", "A recipe for pasta.")

    result = book_compiler.compile_copy_paste(
        "compile a book from all the files containing content about how to raise a child", conn)

    assert result["sources"] == ["parenting.md (page 1/1)"]
    assert "Tips on how to raise a child with patience." in result["markdown"]
    assert "## parenting.md (page 1/1)" in result["markdown"]
    assert "pasta" not in result["markdown"]  # the non-matching file is never included


def test_compile_copy_paste_returns_no_sources_message_when_nothing_matches(tmp_path):
    conn = _tmp_conn(tmp_path)
    _insert(conn, "cooking.md", "A recipe for pasta.")
    result = book_compiler.compile_copy_paste("compile a book about dinosaurs", conn)
    assert result["sources"] == []
    assert "No matching" in result["markdown"]


def test_compile_copy_paste_never_calls_a_model(monkeypatch, tmp_path):
    # no API key needed at all - this mode is pure retrieval. Poisoning both
    # clients guarantees a bug that accidentally calls one fails loudly.
    conn = _tmp_conn(tmp_path)
    _insert(conn, "parenting.md", "Tips on how to raise a child with patience.")

    def _boom(*args, **kwargs):
        raise AssertionError("compile_copy_paste must never call a model client")

    monkeypatch.setattr(book_compiler, "Anthropic", _boom)
    monkeypatch.setattr(book_compiler, "OpenAI", _boom)

    result = book_compiler.compile_copy_paste("raising a child", conn)
    assert result["sources"] == ["parenting.md (page 1/1)"]


def test_compile_copy_paste_preserves_embedded_image_links(tmp_path):
    # unlike compile_book (which sends text to a model and strips images
    # since they're meaningless as prose), copy-paste is a literal
    # reproduction of the stored content - images should render normally.
    conn = _tmp_conn(tmp_path)
    _insert(conn, "book.pdf", "Notes on raising a child.\n\n![handwriting](/images/abc123.jpg)")
    result = book_compiler.compile_copy_paste("raising a child", conn)
    assert "/images/abc123.jpg" in result["markdown"]
