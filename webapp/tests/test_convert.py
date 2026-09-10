import pytest

from backend import convert


def test_text_to_markdown_reads_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello world")
    assert convert.text_to_markdown(str(p)) == "hello world"


def test_convert_to_markdown_dispatches_txt_and_md(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# Title")
    assert convert.convert_to_markdown(str(p), "note.md") == "# Title"


def test_convert_to_markdown_raises_on_unsupported_extension(tmp_path):
    p = tmp_path / "note.xyz"
    p.write_text("data")
    with pytest.raises(ValueError):
        convert.convert_to_markdown(str(p), "note.xyz")


def test_items_to_markdown_strips_control_characters():
    items = [{"kind": "text", "lang": "en", "text": "Bad\x00Char\x07Here"}]
    md = convert.items_to_markdown(items)
    assert "\x00" not in md
    assert "\x07" not in md
    assert "BadCharHere" in md


def test_items_to_markdown_renders_headings_text_and_summary():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "text", "lang": "en", "text": "Hello world"},
        {"kind": "image", "image_bytes": b""},
        {"kind": "handwriting", "image_bytes": b""},
        {"kind": "handwriting", "image_bytes": b""},
        {"kind": "uncertain", "image_bytes": b""},
    ]
    md = convert.items_to_markdown(items)
    assert "## Page 1" in md
    assert "Hello world" in md
    assert "1 image(s)" in md
    assert "2 handwritten/low-confidence region(s)" in md
    assert "1 low-confidence OCR region(s)" in md


def test_items_to_markdown_collapses_many_flagged_items_into_one_summary_line():
    items = [{"kind": "heading", "page": 0}] + [{"kind": "handwriting", "image_bytes": b""}] * 50
    md = convert.items_to_markdown(items)
    assert md.count("Not shown as text") == 1
    assert "50 handwritten" in md


def test_items_to_markdown_gives_each_page_its_own_summary():
    items = [
        {"kind": "heading", "page": 0},
        {"kind": "handwriting", "image_bytes": b""},
        {"kind": "heading", "page": 1},
        {"kind": "text", "lang": "en", "text": "Clean page"},
    ]
    md = convert.items_to_markdown(items)
    assert md.count("Not shown as text") == 1  # only page 1 had a flagged item
    assert "Clean page" in md


def test_pdf_to_markdown_extracts_text_and_page_headings(tmp_path):
    # reuses the synthetic fixture generator from pdf_to_docx_pipeline,
    # reachable because convert.py adds that package to sys.path.
    import make_test_pdf

    pdf_path = make_test_pdf.build(str(tmp_path / "sample.pdf"))
    markdown = convert.pdf_to_markdown(pdf_path, langs="eng+tha")

    assert "## Page 1" in markdown
    assert "## Page 3" in markdown
    assert "Field Visit Notes" in markdown
    assert "Not shown as text" in markdown  # the simulated handwriting on page 3
