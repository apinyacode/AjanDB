"""
Builds a synthetic test PDF that mimics the real-world case we're designing for:
  - Page 1: born-digital (selectable) text, mixed English + Thai       -> zero-OCR path
  - Page 2: a "scanned" page (image only, no text layer), mixed language -> OCR path
  - Page 3: a printed photo/figure + a simulated handwritten note        -> image + handwriting path

This is only test-data generation, not part of the shipped pipeline. Run
directly (`python3 make_test_pdf.py`) to (re)create sample_input.pdf, or
import `build()` to generate a copy elsewhere (used by the pytest suite so
tests don't depend on a large binary fixture checked into git).
"""
import os
import random
import tempfile

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FONT_DIR = os.environ.get("TEST_PDF_FONT_DIR", "/usr/share/fonts/truetype/noto")
DEFAULT_OUT = os.path.join(HERE, "sample_input.pdf")


def build(out_path: str = DEFAULT_OUT, font_dir: str = DEFAULT_FONT_DIR) -> str:
    thai_font = f"{font_dir}/NotoSansThai-Regular.ttf"
    latin_font = f"{font_dir}/NotoSans-Regular.ttf"

    doc = fitz.open()

    # ---------- Page 1: born-digital text (no OCR should be needed) ----------
    page = doc.new_page(width=595, height=842)  # A4
    page.insert_font(fontfile=latin_font, fontname="F-latin")
    page.insert_font(fontfile=thai_font, fontname="F-thai")

    page.insert_text((50, 80), "Field Visit Notes — Ongkharak Site", fontname="F-latin", fontsize=16)
    page.insert_text((50, 120),
        "This page contains real, selectable PDF text. A pipeline should extract",
        fontname="F-latin", fontsize=11)
    page.insert_text((50, 138),
        "this directly, with zero OCR and zero compute cost.",
        fontname="F-latin", fontsize=11)
    page.insert_text((50, 170), "บันทึกภาคสนาม — พื้นที่องครักษ์", fontname="F-thai", fontsize=14)
    page.insert_text((50, 195),
        "ข้อความนี้เป็นข้อความจริงที่สามารถเลือกได้ในไฟล์ PDF",
        fontname="F-thai", fontsize=12)
    page.insert_text((50, 215),
        "ไม่จำเป็นต้องใช้ OCR สำหรับหน้านี้",
        fontname="F-thai", fontsize=12)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # ---------- Page 2: "scanned" page - rendered as a flat image, no text layer ----------
        img = Image.new("RGB", (1240, 1754), "white")  # ~150dpi A4
        d = ImageDraw.Draw(img)
        f_latin_big = ImageFont.truetype(latin_font, 34)
        f_latin = ImageFont.truetype(latin_font, 24)
        f_thai = ImageFont.truetype(thai_font, 26)

        d.text((60, 60), "Cleanup Day — Volunteer Sign-in Sheet", font=f_latin_big, fill="black")
        d.text((60, 130), "This page was scanned from paper. There is no selectable text layer,", font=f_latin, fill="black")
        d.text((60, 160), "so this is the path that needs OCR.", font=f_latin, fill="black")
        d.text((60, 220), "รายชื่ออาสาสมัคร วันทำความสะอาด", font=f_thai, fill="black")
        d.text((60, 260), "หน้านี้ถูกสแกนจากกระดาษ ไม่มีชั้นข้อความ", font=f_thai, fill="black")
        d.text((60, 300), "จึงต้องใช้ OCR ในการอ่าน", font=f_thai, fill="black")

        # slight rotation to simulate a real scan (tests deskew step)
        img = img.rotate(-0.8, expand=True, fillcolor="white")
        scan_page2_path = os.path.join(tmp_dir, "_scan_page2.png")
        img.save(scan_page2_path)

        page2 = doc.new_page(width=595, height=842)
        page2.insert_image(page2.rect, filename=scan_page2_path)

        # ---------- Page 3: born-digital text + an embedded photo + a simulated handwritten note ----------
        page3 = doc.new_page(width=595, height=842)
        page3.insert_font(fontfile=latin_font, fontname="F-latin")
        page3.insert_text((50, 80), "Trail Map Review", fontname="F-latin", fontsize=16)
        page3.insert_text((50, 110), "A photo of the site is inserted below as a picture block.",
                           fontname="F-latin", fontsize=11)

        # a simple synthetic "photo" (not a scanned document - a real picture block)
        photo = Image.new("RGB", (500, 300), (135, 190, 230))  # sky
        pd = ImageDraw.Draw(photo)
        pd.rectangle((0, 200, 500, 300), fill=(90, 140, 70))  # field
        pd.ellipse((60, 60, 200, 190), fill=(150, 110, 70))   # a hut / rock, brown tone
        pd.ellipse((300, 40, 420, 160), fill=(200, 60, 50))   # a red roof / marker
        pd.line((0, 205, 500, 195), fill=(60, 100, 50), width=4)
        photo_path = os.path.join(tmp_dir, "_photo.png")
        photo.save(photo_path)
        page3.insert_image(fitz.Rect(50, 140, 350, 320), filename=photo_path)

        # a simulated "handwritten" note: irregular baseline + jitter, to give the
        # classical-CV heuristic something with non-uniform stroke geometry to catch.
        hw = Image.new("RGB", (500, 160), "white")
        hd = ImageDraw.Draw(hw)
        f_hand = ImageFont.truetype(latin_font, 28)
        text = "check west fence line again - looked loose"
        x = 10
        random.seed(7)
        for ch in text:
            y_jit = 60 + random.randint(-6, 6)
            x_jit = x + random.uniform(-1.5, 1.5)
            hd.text((x_jit, y_jit), ch, font=f_hand, fill="black")
            x += 16 + random.randint(-2, 3)
        handwriting_path = os.path.join(tmp_dir, "_handwriting.png")
        hw.save(handwriting_path)
        page3.insert_image(fitz.Rect(50, 340, 400, 420), filename=handwriting_path)

        doc.save(out_path)
        doc.close()

    return out_path


if __name__ == "__main__":
    written = build()
    print("Wrote", written)
