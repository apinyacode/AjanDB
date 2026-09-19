import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from backend import convert, tts


@pytest.fixture(autouse=True)
def _isolate_images_dir(tmp_path, monkeypatch):
    """Any test that converts a PDF - directly via convert.py, or through a
    full /api/upload or /api/review flow - would otherwise write real .jpg
    files into the actual webapp/data/images/ directory (see
    convert.py's _save_image_as_jpg). Redirected to a throwaway location
    for every test instead, applied globally here rather than per test
    file since the whole test suite can end up exercising this path."""
    monkeypatch.setattr(convert, "IMAGES_DIR", str(tmp_path / "images"))


@pytest.fixture(autouse=True)
def _isolate_audio_dir(tmp_path, monkeypatch):
    """Same reasoning as _isolate_images_dir, for tts.py's generated-speech
    cache under webapp/data/audio/."""
    monkeypatch.setattr(tts, "AUDIO_DIR", str(tmp_path / "audio"))
