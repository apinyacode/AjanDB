import os

from backend import originals


def _fake_file(tmp_path, name="input.pdf", content=b"%PDF-fake-bytes"):
    path = str(tmp_path / name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def test_save_then_load_roundtrips_the_file(tmp_path):
    file_path = _fake_file(tmp_path)

    originals.save(file_path, "book.pdf", 5)
    loaded = originals.load("book.pdf", 5)

    assert loaded is not None
    with open(loaded, "rb") as f:
        assert f.read() == b"%PDF-fake-bytes"
    # the original upload's own file is untouched, not moved
    assert os.path.exists(file_path)


def test_load_returns_none_when_nothing_saved(tmp_path):
    assert originals.load("never-uploaded.pdf", 3) is None


def test_exists_reflects_save_and_delete(tmp_path):
    file_path = _fake_file(tmp_path)
    assert originals.exists("book.pdf", 2) is False

    originals.save(file_path, "book.pdf", 2)
    assert originals.exists("book.pdf", 2) is True

    originals.delete("book.pdf", 2)
    assert originals.exists("book.pdf", 2) is False


def test_delete_is_a_no_op_when_nothing_saved(tmp_path):
    originals.delete("nothing-here.pdf", 1)  # must not raise


def test_save_is_a_no_op_when_already_saved(tmp_path):
    # a multi-batch upload (or a re-run) can call save() more than once for
    # the same book - the second call must not overwrite the first.
    first = _fake_file(tmp_path, "first.pdf", b"FIRST")
    second = _fake_file(tmp_path, "second.pdf", b"SECOND")

    originals.save(first, "book.pdf", 4)
    originals.save(second, "book.pdf", 4)

    with open(originals.load("book.pdf", 4), "rb") as f:
        assert f.read() == b"FIRST"


def test_preserves_the_original_file_extension(tmp_path):
    file_path = _fake_file(tmp_path, "notes.docx", b"docx-bytes")
    originals.save(file_path, "notes.docx", 1)

    loaded = originals.load("notes.docx", 1)
    assert loaded.endswith(".docx")


def test_different_books_get_independent_storage(tmp_path):
    file_a = _fake_file(tmp_path, "a.pdf", b"AAAA")
    file_b = _fake_file(tmp_path, "b.pdf", b"BBBB")
    originals.save(file_a, "a.pdf", 3)
    originals.save(file_b, "b.pdf", 3)

    with open(originals.load("a.pdf", 3), "rb") as f:
        assert f.read() == b"AAAA"
    with open(originals.load("b.pdf", 3), "rb") as f:
        assert f.read() == b"BBBB"


def test_same_filename_different_total_pages_are_independent(tmp_path):
    # total_pages is part of the book-identity key, same as db.list_books()
    file_path = _fake_file(tmp_path)
    originals.save(file_path, "book.pdf", 3)
    assert originals.exists("book.pdf", 3) is True
    assert originals.exists("book.pdf", 10) is False
