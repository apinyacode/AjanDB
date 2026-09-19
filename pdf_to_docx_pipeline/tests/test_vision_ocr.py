"""Structural tests for vision_ocr.py / main_vision.py using a mocked
Anthropic client - there is no ANTHROPIC_API_KEY in this environment, so
these verify JSON parsing, bbox conversion, and docx assembly wiring
without making a real network call, per the known-limitations note in ../CONTEXT.md."""
import json
from types import SimpleNamespace

from pipeline import vision_ocr


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


def _sample_response_json():
    return json.dumps({
        "blocks": [
            {"type": "text", "language": "en", "text": "Hello", "bbox": [0.1, 0.1, 0.5, 0.2]},
            {"type": "photo", "bbox": [0.0, 0.3, 1.0, 0.6]},
            {"type": "handwriting", "text": "note", "bbox": [0.1, 0.7, 0.4, 0.8]},
        ]
    })


def _tiny_png_bytes(w=200, h=100):
    import cv2
    import numpy as np
    img = np.full((h, w, 3), 255, dtype="uint8")
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def test_extract_page_with_vision_parses_blocks_and_converts_bbox(monkeypatch):
    monkeypatch.setattr(
        vision_ocr, "Anthropic",
        lambda api_key=None: _FakeAnthropic(_sample_response_json()),
    )
    png_bytes = _tiny_png_bytes(w=200, h=100)

    blocks = vision_ocr.extract_page_with_vision(png_bytes, model="claude-sonnet-5", api_key="fake")

    assert len(blocks) == 3
    text_block = blocks[0]
    assert text_block["type"] == "text"
    assert text_block["bbox_px"] == (20, 10, 100, 20)  # 0.1*200, 0.1*100, 0.5*200, 0.2*100

    photo_block = blocks[1]
    assert photo_block["bbox_px"] == (0, 30, 200, 60)


def test_extract_page_with_vision_strips_code_fences(monkeypatch):
    fenced = "```json\n" + _sample_response_json() + "\n```"
    monkeypatch.setattr(
        vision_ocr, "Anthropic",
        lambda api_key=None: _FakeAnthropic(fenced),
    )
    png_bytes = _tiny_png_bytes()
    blocks = vision_ocr.extract_page_with_vision(png_bytes, api_key="fake")
    assert len(blocks) == 3


def test_strip_code_fences_handles_plain_json():
    raw = '{"blocks": []}'
    assert vision_ocr._strip_code_fences(raw) == raw


def test_extract_page_with_vision_raises_clear_error_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        vision_ocr.extract_page_with_vision(_tiny_png_bytes())
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "ANTHROPIC_API_KEY" in str(e)


def test_extract_page_with_vision_raises_clear_error_when_no_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        vision_ocr.extract_page_with_vision(_tiny_png_bytes(), provider="openai")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "OPENAI_API_KEY" in str(e)


def test_extract_page_with_vision_uses_openai_when_provider_selected(monkeypatch):
    monkeypatch.setattr(
        vision_ocr, "OpenAI",
        lambda api_key=None: _FakeOpenAI(_sample_response_json()),
    )
    png_bytes = _tiny_png_bytes(w=200, h=100)

    blocks = vision_ocr.extract_page_with_vision(
        png_bytes, provider="openai", model="gpt-4o", api_key="fake")

    assert len(blocks) == 3
    assert blocks[0]["bbox_px"] == (20, 10, 100, 20)


def test_extract_page_with_vision_defaults_openai_model_when_omitted(monkeypatch):
    seen = {}

    class _RecordingOpenAI:
        def __init__(self, api_key=None):
            pass

        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    seen["model"] = kwargs["model"]
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=_sample_response_json()))]
                    )

    monkeypatch.setattr(vision_ocr, "OpenAI", _RecordingOpenAI)
    vision_ocr.extract_page_with_vision(_tiny_png_bytes(), provider="openai", api_key="fake")
    assert seen["model"] == "gpt-4o"


def test_extract_page_with_vision_rejects_unknown_provider():
    try:
        vision_ocr.extract_page_with_vision(_tiny_png_bytes(), provider="bogus", api_key="fake")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "bogus" in str(e)


def test_extract_page_with_vision_repairs_stray_backslash_in_transcribed_text(monkeypatch):
    # A real failure mode: the model transcribes text containing a literal
    # backslash (e.g. "C:\Users\..." or a fraction written "1\2") without
    # doubling it for JSON - raw json.loads rejects this outright with
    # "Invalid \escape", even though a human reader knows exactly what was
    # meant. This is deliberately *not* valid JSON (constructed by hand,
    # not via json.dumps) to reproduce that exact failure.
    broken = '{"blocks": [{"type": "text", "language": "en", ' \
             '"text": "see C:\\Users\\notes", "bbox": [0.1, 0.1, 0.5, 0.2]}]}'
    monkeypatch.setattr(
        vision_ocr, "Anthropic",
        lambda api_key=None: _FakeAnthropic(broken),
    )
    blocks = vision_ocr.extract_page_with_vision(_tiny_png_bytes(), api_key="fake")
    assert len(blocks) == 1
    assert "C:" in blocks[0]["text"] and "Users" in blocks[0]["text"]


def test_extract_page_with_vision_raises_clear_error_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(
        vision_ocr, "Anthropic",
        lambda api_key=None: _FakeAnthropic("This is not JSON at all."),
    )
    try:
        vision_ocr.extract_page_with_vision(_tiny_png_bytes(), api_key="fake")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "wasn't valid JSON" in str(e)
        assert "This is not JSON" in str(e)


class _FakeLogprobItem:
    def __init__(self, token, logprob):
        self.token = token
        self.logprob = logprob


class _FakeOpenAIChoiceWithLogprobs:
    def __init__(self, content, logprob_pairs):
        self.message = _FakeOpenAIMessage(content)
        self.logprobs = SimpleNamespace(
            content=[_FakeLogprobItem(t, lp) for t, lp in logprob_pairs])


class _FakeOpenAIWithLogprobs:
    def __init__(self, content, logprob_pairs, api_key=None):
        def create(**kwargs):
            assert kwargs.get("logprobs") is True
            return SimpleNamespace(choices=[_FakeOpenAIChoiceWithLogprobs(content, logprob_pairs)])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _tokenize_reconstructably(text):
    """Splits `text` into pieces whose concatenation reconstructs it exactly
    - not real BPE, but exercises the same char-offset-mapping logic a real
    tokenization would (see vision_ocr._blocks_with_pixel_bboxes callers'
    downstream use of these offsets in review.py)."""
    import re
    return re.findall(r"\S+|\s+", text)


def test_extract_page_with_vision_logprobs_returns_blocks_text_and_tokens(monkeypatch):
    raw = _sample_response_json()
    logprob_pairs = [(t, -0.1) for t in _tokenize_reconstructably(raw)]
    monkeypatch.setattr(
        vision_ocr, "OpenAI",
        lambda api_key=None: _FakeOpenAIWithLogprobs(raw, logprob_pairs))

    blocks, raw_text, token_logprobs = vision_ocr.extract_page_with_vision_logprobs(
        _tiny_png_bytes(w=200, h=100), api_key="fake")

    assert raw_text == raw
    assert len(blocks) == 3
    assert token_logprobs == logprob_pairs
    assert "".join(t for t, _ in token_logprobs) == raw_text  # offsets are reconstructable


def test_extract_page_with_vision_logprobs_raises_clear_error_when_no_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        vision_ocr.extract_page_with_vision_logprobs(_tiny_png_bytes())
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "OPENAI_API_KEY" in str(e)


def test_repair_invalid_escapes_leaves_valid_escapes_untouched():
    text = r'{"a": "line1\nline2", "b": "quote\"here", "c": "bad\stray"}'
    repaired = vision_ocr._repair_invalid_escapes(text)
    assert '\\n' in repaired
    assert '\\"' in repaired
    assert '\\\\stray' in repaired
    json.loads(repaired)  # doesn't raise
