"""
Stage 4: assemble everything into a .docx, in reading order, with a language
tag on every text block and a clear visual marker on anything that was kept
as an image (pictures, and handwriting we chose not to guess at).
"""
import io
import re
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
import cv2
import numpy as np

_CONTROL_CHARS_RE = re.compile(
    "[" + "".join(chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)) + "]"
)


def _clean(text: str) -> str:
    """Strip XML-incompatible control characters that can occasionally show
    up in extracted PDF text or noisy OCR output."""
    return _CONTROL_CHARS_RE.sub("", text)

LANG_LABELS = {
    "en": "EN", "th": "TH", "zh-cn": "ZH", "zh-tw": "ZH", "ja": "JA",
    "ko": "KO", "fr": "FR", "de": "DE", "es": "ES", "unknown": "?",
}


def _add_lang_tag(paragraph, lang_code: str):
    run = paragraph.add_run(f"[{LANG_LABELS.get(lang_code, lang_code.upper())}] ")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
    run.italic = True


def build_docx(items: list[dict], output_path: str, title: str = "Digitised Document"):
    """
    items: an ordered list of dicts, one per content unit, each shaped like:
      {"kind": "heading", "page": int}
      {"kind": "text", "lang": "en", "text": "..."}
      {"kind": "image", "image_bytes": b"..."}
      {"kind": "handwriting", "image_bytes": b"..."}
    Produced by main.py after stitching together extract.py + ocr.py output.
    """
    doc = Document()
    doc.add_heading(title, level=0)

    current_page = None
    for item in items:
        if item["kind"] == "heading":
            if item["page"] != current_page:
                current_page = item["page"]
                doc.add_heading(f"Page {current_page + 1}", level=1)
            continue

        if item["kind"] == "text":
            p = doc.add_paragraph()
            _add_lang_tag(p, item.get("lang", "unknown"))
            p.add_run(_clean(item["text"]))

        elif item["kind"] == "image":
            doc.add_picture(io.BytesIO(item["image_bytes"]), width=Inches(4.5))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

        elif item["kind"] == "handwriting":
            doc.add_picture(io.BytesIO(item["image_bytes"]), width=Inches(4.0))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            caption = doc.add_paragraph()
            caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = caption.add_run("[Handwritten — kept as image, needs manual review/transcription]")
            run.italic = True
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0xB0, 0x3A, 0x2A)

        elif item["kind"] == "uncertain":
            doc.add_picture(io.BytesIO(item["image_bytes"]), width=Inches(4.0))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            caption = doc.add_paragraph()
            caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = caption.add_run("[Low-confidence OCR — kept as image rather than guessed text]")
            run.italic = True
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    doc.save(output_path)


def crop_to_png_bytes(binary_or_color_image: np.ndarray, bbox: tuple, pad: int = 6) -> bytes:
    """Crop a region out of a page image and encode it back to PNG bytes for
    embedding - used for handwriting regions (and could be reused for any
    OCR-page image sub-region)."""
    h, w = binary_or_color_image.shape[:2]
    left, top, right, bottom = bbox
    left = max(int(left) - pad, 0)
    top = max(int(top) - pad, 0)
    right = min(int(right) + pad, w)
    bottom = min(int(bottom) + pad, h)
    crop = binary_or_color_image[top:bottom, left:right]
    ok, buf = cv2.imencode(".png", crop)
    if not ok:
        raise RuntimeError("Failed to encode cropped region as PNG")
    return buf.tobytes()
