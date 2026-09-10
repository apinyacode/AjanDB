"""
Stage 1: born-digital extraction.

Most PDFs are not uniformly "scanned" or "digital" - a single document can mix
pages that already have a real text layer (free, instant, zero OCR) with pages
that are flat scanned images (need OCR). This module makes that call per page,
and returns content in reading order so downstream stages don't have to guess.

Cost note: this stage does no machine learning and no image rasterization for
pages that already have text - it reads the PDF's internal text objects
directly. This is why "use the text layer when it exists" is the single
biggest lever for reducing compute/energy in a PDF pipeline: a born-digital
page costs microseconds and zero watts of inference; a scanned page costs a
full OCR pass.
"""
from dataclasses import dataclass, field
from typing import Literal
import pymupdf as fitz


@dataclass
class Block:
    kind: Literal["text", "image", "scanned_page"]
    page_number: int
    bbox: tuple
    text: str = ""
    image_bytes: bytes = None
    image_ext: str = "png"
    order: int = 0


def page_has_text_layer(page: fitz.Page, min_chars: int = 12) -> bool:
    """A cheap, reliable check: does this page already carry a real text layer?
    If yes, we never touch OCR for it."""
    return len(page.get_text("text").strip()) >= min_chars


def extract_native_page(page: fitz.Page, page_number: int) -> list[Block]:
    """Pull text + image blocks straight from the PDF's own objects, in
    reading order. Zero OCR involved."""
    blocks = []
    raw = page.get_text("dict", sort=True)["blocks"]
    for b in raw:
        bbox = tuple(b["bbox"])
        if b["type"] == 0:  # text block
            text = "".join(
                span["text"] for line in b["lines"] for span in line["spans"]
            ).strip()
            if text:
                blocks.append(Block(
                    kind="text", page_number=page_number, bbox=bbox,
                    text=text, order=b.get("number", 0),
                ))
        elif b["type"] == 1:  # raster image embedded directly in content stream
            img_bytes = b.get("image")
            if img_bytes:
                blocks.append(Block(
                    kind="image", page_number=page_number, bbox=bbox,
                    image_bytes=img_bytes, order=b.get("number", 0),
                ))

    # Also sweep Document-level embedded images (covers cases where an image
    # is referenced but not surfaced as a "block" by get_text, e.g. full-page
    # background scans placed via an XObject rather than inline content).
    seen_bboxes = {blk.bbox for blk in blocks if blk.kind == "image"}
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        rects = page.get_image_rects(xref)
        for r in rects:
            bbox = tuple(r)
            if bbox in seen_bboxes:
                continue
            try:
                extracted = page.parent.extract_image(xref)
            except Exception:
                continue
            blocks.append(Block(
                kind="image", page_number=page_number, bbox=bbox,
                image_bytes=extracted["image"], image_ext=extracted["ext"],
                order=10_000,  # images picked up this way have no reliable
                               # reading-order number; push near the end of
                               # the page rather than guess
            ))
    blocks.sort(key=lambda blk: blk.order)
    return blocks


def extract_pdf(pdf_path: str) -> list[Block]:
    """Top-level entry point. Returns one flat, ordered list of Blocks across
    the whole document. Pages without a text layer come back as a single
    `scanned_page` placeholder block - stage 2 (ocr.py) is responsible for
    turning those into real text/image/handwriting blocks."""
    doc = fitz.open(pdf_path)
    all_blocks: list[Block] = []
    for i, page in enumerate(doc):
        if page_has_text_layer(page):
            all_blocks.extend(extract_native_page(page, i))
        else:
            all_blocks.append(Block(
                kind="scanned_page", page_number=i, bbox=tuple(page.rect),
            ))
    doc.close()
    return all_blocks


def render_page_for_ocr(pdf_path: str, page_number: int, dpi: int = 300):
    """Rasterize a single page at OCR-friendly resolution. Only called for
    pages that failed the text-layer check - never for born-digital pages."""
    doc = fitz.open(pdf_path)
    pix = doc[page_number].get_pixmap(dpi=dpi)
    png_bytes = pix.tobytes("png")
    doc.close()
    return png_bytes
