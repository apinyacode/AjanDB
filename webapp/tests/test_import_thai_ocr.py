import json
import os

from PIL import Image

from backend import db, qna
from scripts import import_thai_ocr

REVIEW_TEXT = """# page_001  |  confidence: high  |  unsure words: 0  |  spelling fixes: 1
# Compare with page_001.jpg in this folder.

== TEXT (same line breaks as the paper) ==
Dear Ajahn,
How should I meditate?

== CLEAN VERSION (one paragraph) ==
Dear Ajahn, How should I meditate?

== SPELLING FIXES (as written -> corrected) ==
line 2: medidate -> meditate

== NOTES ==
Checked by hand.
"""


def _work_dir(tmp_path):
    """One document in thai_ocr's layout: page 1 reviewed and renamed _DONE,
    page 2 only Claude's JSON, page 3 never transcribed."""
    doc = tmp_path / "work" / "batch_68_1"
    for sub in ("pages", "review", "ai"):
        (doc / sub).mkdir(parents=True)
    for n in (1, 2, 3):
        Image.new("RGB", (40, 60), "white").save(doc / "pages" / f"page_{n:03d}.jpg")
        Image.new("RGB", (30, 20), "white").save(doc / "review" / f"page_{n:03d}.jpg")
    (doc / "review" / "page_001_DONE.txt").write_text(REVIEW_TEXT, encoding="utf-8")
    ai = {"lines": ["Dear Ajahn,", "Is it ⟦normal?⟧"], "corrections": [], "greeting": "Dear Ajahn,",
          "question": "Is it normal?", "closing": "", "continues_from_previous": False,
          "continues_to_next": False, "notes": "", "confidence": "medium"}
    (doc / "ai" / "page_002.json").write_text(json.dumps(ai), encoding="utf-8")
    return tmp_path / "work"


def test_import_keeps_reviews_and_skips_on_rerun(tmp_path):
    conn = db.get_connection(str(tmp_path / "test.sqlite3"))
    work = _work_dir(tmp_path)

    message = import_thai_ocr.import_document(conn, str(work / "batch_68_1"))
    assert message == "batch_68_1: imported 3 pages (1 to review, 1 done, 1 waiting)"

    doc = db.list_qna_documents(conn)[0]
    assert (doc["source_filename"], doc["done"], doc["review"], doc["pending"]) == ("batch_68_1.pdf", 1, 1, 1)
    pages = db.list_qna_pages(conn, doc["id"])
    assert pages[0]["text"] == "Dear Ajahn,\nHow should I meditate?"
    assert pages[0]["fixes"] == "line 2: medidate -> meditate"
    assert pages[0]["notes"] == "Checked by hand."
    assert pages[1]["text"] == "Dear Ajahn,\nIs it ⟦normal?⟧" and pages[1]["confidence"] == "medium"
    assert pages[2]["status"] == "pending"
    # Cropped image comes from review/, full page from pages/.
    assert Image.open(qna.image_path(doc["id"], 1, "crop")).size == (30, 20)
    assert Image.open(qna.image_path(doc["id"], 1, "full")).size == (40, 60)

    assert import_thai_ocr.import_document(conn, str(work / "batch_68_1")).endswith("already in QnA - skipped")
    assert len(db.list_qna_documents(conn)) == 1
    conn.close()


def test_import_reads_older_crops_folder(tmp_path):
    conn = db.get_connection(str(tmp_path / "test.sqlite3"))
    doc = tmp_path / "work" / "old"
    (doc / "pages").mkdir(parents=True)
    (doc / "crops").mkdir()
    Image.new("RGB", (40, 60), "white").save(doc / "pages" / "page_001.jpg")
    Image.new("RGB", (25, 25), "white").save(doc / "crops" / "page_001.jpg")
    import_thai_ocr.import_document(conn, str(doc))
    document_id = db.list_qna_documents(conn)[0]["id"]
    assert Image.open(qna.image_path(document_id, 1, "crop")).size == (25, 25)
    assert os.path.isfile(qna.image_path(document_id, 1, "full"))
    conn.close()
