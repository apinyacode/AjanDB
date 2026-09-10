import pymupdf as fitz

from pipeline import extract


def test_page_has_text_layer_true_for_born_digital(sample_pdf_path):
    doc = fitz.open(sample_pdf_path)
    assert extract.page_has_text_layer(doc[0]) is True
    doc.close()


def test_page_has_text_layer_false_for_scanned_page(sample_pdf_path):
    doc = fitz.open(sample_pdf_path)
    assert extract.page_has_text_layer(doc[1]) is False
    doc.close()


def test_extract_pdf_classifies_all_three_pages(sample_pdf_path):
    blocks = extract.extract_pdf(sample_pdf_path)
    kinds_by_page = {}
    for b in blocks:
        kinds_by_page.setdefault(b.page_number, set()).add(b.kind)

    assert kinds_by_page[0] == {"text"}
    assert kinds_by_page[1] == {"scanned_page"}
    assert "text" in kinds_by_page[2]
    assert "image" in kinds_by_page[2]


def test_extract_pdf_preserves_reading_order_on_page1(sample_pdf_path):
    blocks = extract.extract_pdf(sample_pdf_path)
    page1_text = [b.text for b in blocks if b.page_number == 0 and b.kind == "text"]
    assert page1_text
    assert "Field Visit Notes" in page1_text[0]
    # Thai block comes after the English intro, matching source layout
    thai_index = next(i for i, t in enumerate(page1_text) if "บันทึก" in t)
    english_index = next(i for i, t in enumerate(page1_text) if "Field Visit" in t)
    assert english_index < thai_index


def test_render_page_for_ocr_returns_png_bytes(sample_pdf_path):
    png_bytes = extract.render_page_for_ocr(sample_pdf_path, 1, dpi=150)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"


def test_scanned_page_block_has_no_text(sample_pdf_path):
    blocks = extract.extract_pdf(sample_pdf_path)
    scanned = [b for b in blocks if b.kind == "scanned_page"]
    assert len(scanned) == 1
    assert scanned[0].text == ""
