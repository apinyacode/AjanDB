from docx import Document

from pipeline import main


def test_run_end_to_end_stats(sample_pdf_path, tmp_path):
    out = tmp_path / "output.docx"
    stats = main.run(sample_pdf_path, str(out), langs="eng+tha")

    assert out.exists()
    # page 1 and page 3 are born-digital -> zero OCR; only page 2 needs OCR
    assert stats["ocr_pages"] == 1
    assert stats["native_text_blocks"] > 0
    assert stats["ocr_lines"] > 0
    # the simulated handwritten note on page 3 must be caught
    assert stats["handwritten_lines"] >= 1
    # the synthetic "photo" on page 3 must be kept as an image, not OCR'd
    assert stats["images"] >= 1


def test_run_output_is_valid_docx_with_expected_pages(sample_pdf_path, tmp_path):
    out = tmp_path / "output.docx"
    main.run(sample_pdf_path, str(out), langs="eng+tha")

    doc = Document(str(out))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert "Page 1" in headings
    assert "Page 2" in headings
    assert "Page 3" in headings


def test_run_never_ocrs_born_digital_pages(sample_pdf_path, tmp_path, monkeypatch):
    """Regression guard for the pipeline's core cost claim: born-digital
    pages must never be rasterized/OCR'd."""
    from pipeline import extract as extract_module

    calls = []
    original = extract_module.render_page_for_ocr

    def spy(pdf_path, page_number, dpi=300):
        calls.append(page_number)
        return original(pdf_path, page_number, dpi=dpi)

    monkeypatch.setattr(extract_module, "render_page_for_ocr", spy)

    out = tmp_path / "output.docx"
    main.run(sample_pdf_path, str(out), langs="eng+tha")

    assert calls == [1]  # only the scanned page (index 1) was rasterized
