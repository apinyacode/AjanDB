import os

from backend import sources


def _fake_pdf(tmp_path, name="input.pdf", content=b"%PDF-fake-bytes"):
    path = str(tmp_path / name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def test_save_then_load_roundtrips_pdf_and_settings(tmp_path):
    pdf_path = _fake_pdf(tmp_path)
    settings = {"engine": "vision", "provider": "anthropic", "next_index": 0}

    sources.save(pdf_path, "book.pdf", 5, settings)
    loaded = sources.load("book.pdf", 5)

    assert loaded is not None
    saved_pdf_path, saved_settings = loaded
    assert os.path.exists(saved_pdf_path)
    with open(saved_pdf_path, "rb") as f:
        assert f.read() == b"%PDF-fake-bytes"
    assert saved_settings == settings
    # the original upload's own file is untouched, not moved
    assert os.path.exists(pdf_path)


def test_load_returns_none_when_nothing_saved(tmp_path):
    assert sources.load("never-uploaded.pdf", 3) is None


def test_exists_reflects_save_and_delete(tmp_path):
    pdf_path = _fake_pdf(tmp_path)
    assert sources.exists("book.pdf", 2) is False

    sources.save(pdf_path, "book.pdf", 2, {"engine": "classical", "next_index": 0})
    assert sources.exists("book.pdf", 2) is True

    sources.delete("book.pdf", 2)
    assert sources.exists("book.pdf", 2) is False


def test_delete_is_a_no_op_when_nothing_saved(tmp_path):
    sources.delete("nothing-here.pdf", 1)  # must not raise


def test_update_overwrites_the_full_settings(tmp_path):
    pdf_path = _fake_pdf(tmp_path)
    sources.save(pdf_path, "book.pdf", 4, {"engine": "classical", "langs": "eng+tha",
                                            "category": None, "next_index": 0})

    # a category decided partway through (see review.approve()'s
    # auto-suggestion) must survive being persisted here, not just next_index -
    # a partial update that only touched next_index would lose it.
    sources.update("book.pdf", 4, {"engine": "classical", "langs": "eng+tha",
                                    "category": "Field Notes", "next_index": 2})

    _, settings = sources.load("book.pdf", 4)
    assert settings["next_index"] == 2
    assert settings["category"] == "Field Notes"
    assert settings["langs"] == "eng+tha"


def test_update_is_a_no_op_when_nothing_saved(tmp_path):
    sources.update("nothing-here.pdf", 1, {"next_index": 5})  # must not raise
    assert sources.load("nothing-here.pdf", 1) is None


def test_different_books_get_independent_storage(tmp_path):
    pdf_a = _fake_pdf(tmp_path, "a.pdf", b"AAAA")
    pdf_b = _fake_pdf(tmp_path, "b.pdf", b"BBBB")
    sources.save(pdf_a, "a.pdf", 3, {"engine": "classical", "next_index": 0})
    sources.save(pdf_b, "b.pdf", 3, {"engine": "vision", "next_index": 1})

    loaded_a = sources.load("a.pdf", 3)
    loaded_b = sources.load("b.pdf", 3)
    with open(loaded_a[0], "rb") as f:
        assert f.read() == b"AAAA"
    with open(loaded_b[0], "rb") as f:
        assert f.read() == b"BBBB"
    assert loaded_a[1]["engine"] == "classical"
    assert loaded_b[1]["engine"] == "vision"


def test_same_filename_different_total_pages_are_independent(tmp_path):
    # total_pages is part of the book-identity key, same as db.list_books()
    pdf_path = _fake_pdf(tmp_path)
    sources.save(pdf_path, "book.pdf", 3, {"engine": "classical", "next_index": 0})
    assert sources.exists("book.pdf", 3) is True
    assert sources.exists("book.pdf", 10) is False
