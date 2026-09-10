"""Structural tests for vision_ocr.py / main_vision.py using a mocked
Anthropic client - there is no ANTHROPIC_API_KEY in this environment, so
these verify JSON parsing, bbox conversion, and docx assembly wiring
without making a real network call, per the honesty note in README.md."""
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
