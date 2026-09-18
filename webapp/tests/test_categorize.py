from types import SimpleNamespace

from backend import categorize


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


def test_suggest_category_returns_none_for_empty_text():
    assert categorize.suggest_category("") is None
    assert categorize.suggest_category("   ") is None


def test_suggest_category_returns_none_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert categorize.suggest_category("Some real document content here.") is None


def test_suggest_category_returns_none_for_unknown_provider():
    assert categorize.suggest_category("text", provider="bogus", api_key="fake") is None


def test_suggest_category_calls_anthropic_and_cleans_label(monkeypatch):
    monkeypatch.setattr(
        categorize, "Anthropic",
        lambda api_key=None: _FakeAnthropic('"Dharma Talks"'),
    )
    label = categorize.suggest_category("A talk about mindfulness.", api_key="fake")
    assert label == "Dharma Talks"


def test_suggest_category_calls_openai_when_provider_selected(monkeypatch):
    monkeypatch.setattr(
        categorize, "OpenAI",
        lambda api_key=None: _FakeOpenAI("Recipes"),
    )
    label = categorize.suggest_category(
        "Ingredients: garlic, olive oil.", provider="openai", api_key="fake")
    assert label == "Recipes"


def test_suggest_category_strips_embedded_images_from_the_excerpt(monkeypatch):
    captured = {}

    def _capturing_create(self, **kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return SimpleNamespace(content=[_FakeTextBlock("Field Notes")])

    monkeypatch.setattr(_FakeMessages, "create", _capturing_create)
    monkeypatch.setattr(categorize, "Anthropic", lambda api_key=None: _FakeAnthropic(""))

    huge_fake_base64 = "A" * 5000
    text = f"A field report about crops.\n\n![image](data:image/png;base64,{huge_fake_base64})"
    label = categorize.suggest_category(text, api_key="fake")

    assert label == "Field Notes"
    assert huge_fake_base64 not in captured["prompt"]
    assert "A field report about crops" in captured["prompt"]


def test_suggest_category_returns_none_when_model_call_raises(monkeypatch):
    class _BoomAnthropic:
        def __init__(self, api_key=None):
            raise RuntimeError("network error")

    monkeypatch.setattr(categorize, "Anthropic", _BoomAnthropic)
    assert categorize.suggest_category("Some content.", api_key="fake") is None
