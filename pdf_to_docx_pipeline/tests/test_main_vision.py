"""End-to-end test of main_vision.py with the vision-LLM call mocked out -
no ANTHROPIC_API_KEY is available in this environment (see README.md's
honesty note), so this exercises everything else: born-digital extraction,
bbox-to-pixel-crop math, and docx assembly for the vision engine."""
from docx import Document

from pipeline import main_vision


def _fake_extract_page_with_vision(png_bytes, model=None):
    from PIL import Image
    import io as _io

    img = Image.open(_io.BytesIO(png_bytes))
    w, h = img.size
    return [
        {"type": "text", "language": "en", "text": "Mocked OCR line",
         "bbox_px": (0, 0, w // 2, h // 4)},
        {"type": "photo", "bbox_px": (0, h // 2, w, h)},
        {"type": "handwriting", "text": "draft note",
         "bbox_px": (0, h // 4, w // 2, h // 2)},
    ]


def test_main_vision_run_end_to_end(sample_pdf_path, tmp_path, monkeypatch):
    monkeypatch.setattr(
        main_vision.vision_ocr, "extract_page_with_vision",
        _fake_extract_page_with_vision,
    )
    out = tmp_path / "output.docx"
    stats = main_vision.run(sample_pdf_path, str(out))

    assert out.exists()
    assert stats["pages_sent_to_vision_model"] == 1
    assert stats["vision_text_blocks"] == 1
    assert stats["handwritten_blocks"] == 1
    assert stats["images"] >= 1  # native photo on page 3 + mocked photo block
    assert stats["native_text_blocks"] > 0

    doc = Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Mocked OCR line" in text
    assert "(draft transcription) draft note" in text
