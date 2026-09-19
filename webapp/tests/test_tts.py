import os

import pytest

from backend import tts


def test_voice_for_text_picks_thai_voice_for_thai_script():
    assert tts._voice_for_text("สวัสดีครับ") == tts._VOICE_THAI


def test_voice_for_text_picks_english_voice_for_latin_script():
    assert tts._voice_for_text("Hello there") == tts._VOICE_ENGLISH


def test_synthesize_raises_without_key_or_region(monkeypatch):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    with pytest.raises(RuntimeError):
        tts.synthesize("hello")


def test_get_or_synthesize_only_calls_provider_once_for_repeated_text(monkeypatch):
    calls = []

    def fake_synthesize(text, region=None, api_key=None):
        calls.append(text)
        return b"fake-mp3-bytes"

    monkeypatch.setattr(tts, "synthesize", fake_synthesize)

    url1 = tts.get_or_synthesize("Hello there", region="eastus", api_key="fake-key")
    url2 = tts.get_or_synthesize("Hello there", region="eastus", api_key="fake-key")

    assert url1 == url2
    assert len(calls) == 1  # second call was a cache hit, no re-synthesis
    cached_path = os.path.join(tts.AUDIO_DIR, os.path.basename(url1))
    assert os.path.exists(cached_path)
    with open(cached_path, "rb") as f:
        assert f.read() == b"fake-mp3-bytes"


def test_get_or_synthesize_different_text_produces_different_urls(monkeypatch):
    monkeypatch.setattr(tts, "synthesize", lambda text, region=None, api_key=None: b"bytes")

    url1 = tts.get_or_synthesize("First paragraph")
    url2 = tts.get_or_synthesize("Second paragraph")

    assert url1 != url2


def test_synthesize_uses_access_token_and_ssml(monkeypatch):
    """Full path through synthesize(): a fake requests.post captures the
    token-fetch and the actual TTS call, mocked the same way
    test_book_compiler.py mocks provider SDK calls - no real network call,
    no billed Azure request."""
    captured = {}

    class _FakeResponse:
        def __init__(self, text=None, content=None):
            self.text = text
            self.content = content

        def raise_for_status(self):
            pass

    def fake_post(url, headers=None, data=None, timeout=None):
        if "issueToken" in url:
            captured["token_headers"] = headers
            return _FakeResponse(text="fake-token")
        captured["tts_url"] = url
        captured["tts_headers"] = headers
        captured["tts_body"] = data
        return _FakeResponse(content=b"fake-mp3-bytes")

    monkeypatch.setattr(tts.requests, "post", fake_post)

    audio = tts.synthesize("สวัสดี", region="eastus", api_key="fake-key")

    assert audio == b"fake-mp3-bytes"
    assert captured["token_headers"]["Ocp-Apim-Subscription-Key"] == "fake-key"
    assert captured["tts_headers"]["Authorization"] == "Bearer fake-token"
    assert tts._VOICE_THAI in captured["tts_body"].decode("utf-8")
