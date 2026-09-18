import os

import cv2
import numpy as np
import pytest

from backend import convert


def _real_png_bytes(color=(0, 0, 255)):
    """A tiny real, decodable image - fake placeholder bytes like b"fake"
    no longer work now that convert.py actually decodes/re-encodes images
    (cv2.imdecode legitimately rejects garbage input, unlike the old
    base64-wrap-anything approach)."""
    img = np.full((4, 4, 3), color, dtype="uint8")
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def test_text_to_markdown_returns_single_chunk_for_short_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello world")
    chunks = convert.text_to_markdown(str(p))
    assert len(chunks) == 1
    assert chunks[0] == {"page_number": 1, "markdown": "hello world",
                          "confidence": None, "needs_review": False, "flagged_snippets": []}


def test_text_to_markdown_splits_long_file_into_multiple_chunks(tmp_path):
    p = tmp_path / "note.txt"
    paragraphs = [f"Paragraph {i} " + ("word " * 200) for i in range(10)]
    p.write_text("\n\n".join(paragraphs))
    chunks = convert.text_to_markdown(str(p))
    assert len(chunks) > 1
    assert [c["page_number"] for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c["confidence"] is None and c["needs_review"] is False for c in chunks)


def test_convert_to_markdown_dispatches_txt_and_md(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# Title")
    chunks = convert.convert_to_markdown(str(p), "note.md")
    assert chunks == [{"page_number": 1, "markdown": "# Title",
                        "confidence": None, "needs_review": False, "flagged_snippets": []}]


def test_convert_to_markdown_raises_on_unsupported_extension(tmp_path):
    p = tmp_path / "note.xyz"
    p.write_text("data")
    with pytest.raises(ValueError):
        convert.convert_to_markdown(str(p), "note.xyz")


def test_convert_to_markdown_rejects_unknown_engine_for_pdf(tmp_path):
    p = tmp_path / "note.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ValueError):
        convert.convert_to_markdown(str(p), "note.pdf", engine="bogus")


def test_items_to_chunks_strips_control_characters():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "Bad\x00Char\x07Here", "confidence": 100.0},
    ]
    chunks = convert.items_to_chunks(items)
    assert "\x00" not in chunks[0]["markdown"]
    assert "\x07" not in chunks[0]["markdown"]
    assert "BadCharHere" in chunks[0]["markdown"]


def test_items_to_chunks_one_chunk_per_page_with_averaged_confidence():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "Hello", "confidence": 90.0},
        {"kind": "text", "lang": "en", "text": "world", "confidence": 80.0},
        {"kind": "heading", "page": 1},
        {"kind": "text", "lang": "en", "text": "Second page", "confidence": 100.0},
    ]
    chunks = convert.items_to_chunks(items)
    assert len(chunks) == 2
    assert chunks[0]["page_number"] == 1
    assert "Hello" in chunks[0]["markdown"] and "world" in chunks[0]["markdown"]
    assert chunks[0]["confidence"] == 85.0
    assert chunks[0]["needs_review"] is False
    assert chunks[1]["page_number"] == 2
    assert chunks[1]["confidence"] == 100.0


def test_items_to_chunks_flags_low_confidence_page_for_review():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "garbled", "confidence": 50.0},
    ]
    chunks = convert.items_to_chunks(items)
    assert chunks[0]["needs_review"] is True


def test_items_to_chunks_flags_handwriting_and_uncertain_regardless_of_confidence():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "Clean high-confidence text", "confidence": 99.0},
        {"kind": "handwriting", "image_bytes": _real_png_bytes()},
    ]
    chunks = convert.items_to_chunks(items)
    assert chunks[0]["needs_review"] is True
    assert "![handwriting](/images/" in chunks[0]["markdown"]
    assert chunks[0]["markdown"].endswith(".jpg)")
    assert chunks[0]["flagged_snippets"]  # the embedded image itself is flagged


def test_items_to_chunks_plain_image_does_not_trigger_review():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "Clean text", "confidence": 100.0},
        {"kind": "image", "image_bytes": _real_png_bytes()},
    ]
    chunks = convert.items_to_chunks(items)
    assert chunks[0]["needs_review"] is False
    assert "![image](/images/" in chunks[0]["markdown"]
    assert chunks[0]["flagged_snippets"] == []  # a plain photo isn't flagged for review


def test_items_to_chunks_completely_blank_page_gets_placeholder():
    items = [{"kind": "heading", "page": 0}]
    chunks = convert.items_to_chunks(items)
    assert "No extractable text" in chunks[0]["markdown"]
    assert chunks[0]["confidence"] is None


def test_items_to_chunks_page_with_only_an_image_reports_it_not_a_placeholder():
    items = [{"kind": "heading", "page": 0}, {"kind": "image", "image_bytes": _real_png_bytes()}]
    chunks = convert.items_to_chunks(items)
    assert "![image](/images/" in chunks[0]["markdown"]
    assert "No extractable text" not in chunks[0]["markdown"]


def test_items_to_chunks_writes_a_real_jpg_file_that_reuses_on_same_bytes():
    image_bytes = _real_png_bytes()
    items = [{"kind": "heading", "page": 0}, {"kind": "image", "image_bytes": image_bytes}]

    chunks = convert.items_to_chunks(items)
    filename = chunks[0]["markdown"].split("(/images/")[1].rstrip(")")
    path = os.path.join(convert.IMAGES_DIR, filename)
    assert os.path.exists(path)
    assert filename.endswith(".jpg")
    mtime_first = os.path.getmtime(path)

    # a second page with the exact same image bytes should reuse the file,
    # not write a duplicate
    chunks2 = convert.items_to_chunks(items)
    filename2 = chunks2[0]["markdown"].split("(/images/")[1].rstrip(")")
    assert filename2 == filename
    assert os.path.getmtime(path) == mtime_first


def test_items_to_chunks_skips_embedding_when_image_bytes_undecodable():
    # Defensive case: if a doc_item's bytes genuinely aren't a real image,
    # don't emit a broken image reference - just drop it, still counted
    # for needs_review.
    items = [{"kind": "heading", "page": 0}, {"kind": "handwriting", "image_bytes": b"not an image"}]
    chunks = convert.items_to_chunks(items)
    assert "![" not in chunks[0]["markdown"]
    assert chunks[0]["needs_review"] is True


def test_items_to_chunks_skips_embedding_when_image_bytes_missing():
    items = [{"kind": "heading", "page": 0}, {"kind": "handwriting", "image_bytes": b""}]
    chunks = convert.items_to_chunks(items)
    assert "![" not in chunks[0]["markdown"]
    assert chunks[0]["needs_review"] is True


def test_items_to_chunks_flags_lines_with_confidence_below_100():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "Perfect native text", "confidence": 100.0},
        {"kind": "text", "lang": "en", "text": "guessed ocr line", "confidence": 92.0},
    ]
    chunks = convert.items_to_chunks(items)
    assert chunks[0]["flagged_snippets"] == ["guessed ocr line"]
    # every flagged snippet must be an exact substring of the chunk's own
    # markdown, so the frontend can find and highlight it there
    assert chunks[0]["flagged_snippets"][0] in chunks[0]["markdown"]


def test_items_to_chunks_no_flagged_snippets_when_everything_is_confident():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "Native text", "confidence": 100.0},
    ]
    chunks = convert.items_to_chunks(items)
    assert chunks[0]["flagged_snippets"] == []


def test_items_to_chunks_embeds_images_in_reading_order_between_text():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "Before the image", "confidence": 100.0},
        {"kind": "image", "image_bytes": _real_png_bytes()},
        {"kind": "text", "lang": "en", "text": "After the image", "confidence": 100.0},
    ]
    chunks = convert.items_to_chunks(items)
    markdown = chunks[0]["markdown"]
    assert markdown.index("Before the image") < markdown.index("![image]") < markdown.index("After the image")


def test_pdf_to_markdown_extracts_text_as_per_page_chunks(tmp_path):
    # reuses the synthetic fixture generator from pdf_to_docx_pipeline,
    # reachable because convert.py adds that package to sys.path.
    import make_test_pdf

    pdf_path = make_test_pdf.build(str(tmp_path / "sample.pdf"))
    chunks = convert.pdf_to_markdown(pdf_path, langs="eng+tha")

    assert len(chunks) == 3
    assert chunks[0]["page_number"] == 1
    assert "Field Visit Notes" in chunks[0]["markdown"]
    assert chunks[0]["confidence"] == 100.0  # born-digital text, not OCR
    assert chunks[0]["needs_review"] is False
    assert chunks[0]["flagged_snippets"] == []  # nothing guessed on this page

    page3 = chunks[2]
    # page 3 has a real embedded photo (kind="image", not flagged) and the
    # simulated handwritten note (kind="handwriting", flagged) - both must
    # be embedded as actual images rather than a placeholder note.
    assert page3["markdown"].count("![") == 2
    assert "![image](/images/" in page3["markdown"]
    assert "![handwriting](/images/" in page3["markdown"]
    assert page3["needs_review"] is True
    assert any(s.startswith("![handwriting]") for s in page3["flagged_snippets"])
    assert not any(s.startswith("![image]") for s in page3["flagged_snippets"])


def test_pdf_to_markdown_reports_progress_per_page(tmp_path):
    import make_test_pdf

    pdf_path = make_test_pdf.build(str(tmp_path / "sample.pdf"))
    seen = []
    convert.pdf_to_markdown(pdf_path, langs="eng+tha", progress=lambda p, t: seen.append((p, t)))
    assert seen == [(0, 3), (1, 3), (2, 3)]


def test_get_pdf_page_count(tmp_path):
    import make_test_pdf

    pdf_path = make_test_pdf.build(str(tmp_path / "sample.pdf"))
    assert convert.get_pdf_page_count(pdf_path) == 3


def test_extract_single_page_pdf_produces_a_one_page_file_with_matching_content(tmp_path):
    import make_test_pdf

    pdf_path = make_test_pdf.build(str(tmp_path / "sample.pdf"))
    single_page_path = convert.extract_single_page_pdf(pdf_path, 0)
    try:
        assert convert.get_pdf_page_count(single_page_path) == 1
        chunks = convert.pdf_to_markdown(single_page_path, langs="eng+tha")
        assert len(chunks) == 1
        assert "Field Visit Notes" in chunks[0]["markdown"]
    finally:
        os.unlink(single_page_path)


def test_render_page_preview_returns_png_bytes(tmp_path):
    import make_test_pdf

    pdf_path = make_test_pdf.build(str(tmp_path / "sample.pdf"))
    png_bytes = convert.render_page_preview(pdf_path, 0)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
