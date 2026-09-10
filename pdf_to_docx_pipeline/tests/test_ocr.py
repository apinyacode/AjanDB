import numpy as np

from pipeline import extract, ocr


def test_normalize_thai_spacing_collapses_intercharacter_gaps():
    spaced = "ร า ย ชื ่ อ"
    assert ocr.normalize_thai_spacing(spaced) == "รายชื่อ"


def test_normalize_thai_spacing_preserves_thai_english_boundary():
    text = "ข้อความ PDF ต่อไป"
    result = ocr.normalize_thai_spacing(text)
    assert " PDF " in result


def test_normalize_thai_spacing_noop_on_pure_english():
    text = "This is plain English text"
    assert ocr.normalize_thai_spacing(text) == text


def test_tag_language_detects_english():
    assert ocr.tag_language("This is a full English sentence about the weather.") == "en"


def test_tag_language_detects_thai():
    assert ocr.tag_language("ข้อความนี้เป็นข้อความจริงที่สามารถเลือกได้ในไฟล์") == "th"


def test_tag_language_returns_unknown_on_empty_string():
    assert ocr.tag_language("") == "unknown"


def test_process_scanned_page_extracts_lines_and_no_pictures(sample_pdf_path):
    png_bytes = extract.render_page_for_ocr(sample_pdf_path, 1, dpi=200)
    result = ocr.process_scanned_page(png_bytes, langs="eng+tha")

    assert result["lines"], "expected at least one OCR'd line on the scanned page"
    assert isinstance(result["processed"], np.ndarray)
    # page 2 is pure sign-in-sheet text, no photo content
    assert result["pictures"] == []

    joined = " ".join(line["text"] for line in result["lines"])
    assert "Cleanup" in joined or "Sign" in joined


def test_process_scanned_page_detects_picture_region(sample_pdf_path):
    png_bytes = extract.render_page_for_ocr(sample_pdf_path, 2, dpi=200)
    result = ocr.process_scanned_page(png_bytes, langs="eng+tha")
    assert len(result["pictures"]) >= 1


def test_segment_regions_returns_two_lists():
    import cv2

    color = np.full((400, 600, 3), 255, dtype=np.uint8)
    cv2.putText(color, "Hello World", (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    cv2.rectangle(color, (100, 200), (400, 350), (200, 50, 50), -1)  # colourful "photo"

    text_bboxes, picture_bboxes = ocr.segment_regions(color)
    assert isinstance(text_bboxes, list)
    assert isinstance(picture_bboxes, list)
    assert len(picture_bboxes) >= 1
