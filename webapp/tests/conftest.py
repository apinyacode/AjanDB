import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from backend import content_studio, convert, originals, sources, tts


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


@pytest.fixture(autouse=True)
def _isolate_sources_dir(tmp_path, monkeypatch):
    """Same reasoning as _isolate_images_dir, for sources.py's durable
    per-review-session PDF copies under webapp/data/sources/."""
    monkeypatch.setattr(sources, "SOURCES_DIR", str(tmp_path / "sources"))


@pytest.fixture(autouse=True)
def _isolate_originals_dir(tmp_path, monkeypatch):
    """Same reasoning as _isolate_images_dir, for originals.py's permanent
    per-book copies of the original uploaded file under
    webapp/data/originals/."""
    monkeypatch.setattr(originals, "ORIGINALS_DIR", str(tmp_path / "originals"))


@pytest.fixture(autouse=True)
def _isolate_generated_media_dirs(tmp_path, monkeypatch):
    """Same reasoning as _isolate_images_dir, for content_studio.py's
    generated images/videos under webapp/data/generated_images/ and
    webapp/data/generated_videos/."""
    monkeypatch.setattr(content_studio, "IMAGES_DIR", str(tmp_path / "generated_images"))
    monkeypatch.setattr(content_studio, "VIDEOS_DIR", str(tmp_path / "generated_videos"))


@pytest.fixture(autouse=True)
def _clear_provider_env_keys(monkeypatch):
    """review.py's automatic cross-check (see
    _cross_provider_disagreement_flags) fires for *any* review-flow page
    below 100% confidence whenever ANTHROPIC_API_KEY/OPENAI_API_KEY is set
    in the backend's own environment - deliberately, since there's no
    browser-supplied-key path for it (see that function's docstring). That
    makes the whole suite sensitive to whatever happens to be exported in
    the shell running it: without this, a real key present on a dev/CI
    machine would turn ordinary review-flow tests into real, billed network
    calls. Cleared globally; a test that wants the "key is configured" path
    sets one back with monkeypatch.setenv."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
