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
