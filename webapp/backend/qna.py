"""
QnA: handwritten question slips (Thai/English) -> reviewed text.

A separate flow from Data Store's page-by-page review, built for one specific
kind of document: scanned slips that lay practitioners hand to a teacher,
usually one question per page. Every page is transcribed by Claude up front
(in the background), then a person checks each page against its scan at their
own pace and marks it done. Nothing is final until a human has looked at it,
so pages carry a status - pending (not transcribed yet), review (transcribed,
waiting for a person), done (checked), error (transcription failed, retryable).

Why not reuse the vision engine (pipeline/vision_ocr.py)? Its JSON output is
hand-formatted by the model and has needed two repair heuristics already (see
CONTEXT.md "Vision-LLM JSON repair"); this uses the API's own structured
output instead, so the response always matches PageTranscription. It also
asks for things the generic engine doesn't: the writer's line breaks kept
exactly (so a reviewer can compare line by line), spelling corrected with
every fix listed, and uncertain words marked inline as ⟦word?⟧.

Page images live under data/qna/<document_id>/: page_001.jpg (full page, sent
to Claude for context and shown on request) and page_001_crop.jpg (just the
handwriting, enlarged - what Claude reads from and what the reviewer sees).
"""
import base64
import csv
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import anthropic
import numpy as np
import pymupdf as fitz
from docx import Document
from docx.shared import Cm, Pt
from PIL import Image
from pydantic import BaseModel, Field

from . import db

QNA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "qna")

MODEL = "claude-opus-5-5"
RENDER_DPI = 200            # the slips were scanned at 200 dpi, so rendering higher adds nothing
FULL_PAGE_MAX_SIDE = 1400   # the full page is only context for the model, so a smaller copy is enough
CONCURRENT_PAGES = 4        # pages transcribed at once - faster than one by one, gentle on rate limits

# Cropping: dark pen strokes, ignoring the scan's edges and straight lines.
INK_DARKNESS = 150      # 0 = black, 255 = white
EDGE_TOP_BOTTOM = 0.02  # some slips start right at the top of the scan, so only a thin band
EDGE_SIDES = 0.04       # scanner edge lines sit a few percent in from the sides
CROP_PADDING = 50
MAX_CROP_SIDE = 2000

UNSURE_PATTERN = re.compile(r"⟦(.+?)⟧")

SYSTEM_PROMPT = """You transcribe scanned handwritten Thai documents for a digital archive.

The documents are question slips that lay practitioners wrote to a Buddhist meditation \
teacher. They usually open with a salutation such as "กราบนมัสการพระอาจารย์..." and close \
with thanks such as "ขอบพระคุณเจ้าค่ะ". They often contain Pali and Dhamma vocabulary \
(e.g. สติปัฏฐาน, จิตตานุปัสสนา, กิเลส, สมาธิ, วิปัสสนา) and names from the Pali canon \
(e.g. องคุลีมาล). Some slips contain English words or are entirely in English (often \
addressed "Dear Ajahn"); transcribe those as written.

A person will check your transcription line by line against the scan, so:
- In `lines`, reproduce every handwritten line in order, keeping the writer's own line \
breaks. Do not merge or split lines.
- Correct spelling mistakes (including misspelled Pali/Dhamma terms and wrong tone marks), \
and list every correction in `corrections` with what was actually written. Only fix \
spelling: do not change word choice, grammar, or the writer's style, and do not "correct" \
informal or polite spoken forms such as ค่ะ/คะ/เจ้าค่ะ unless clearly misspelled.
- Wrap every word you cannot read with confidence like ⟦word?⟧, giving your best guess \
inside. Use ⟦?⟧ when you cannot guess at all. Marking a word you were unsure of is much \
better than a confident wrong reading.
- Keep Thai digits as Thai digits and Arabic digits as Arabic digits.
- Skip scanner marks, stamps and printed letterheads that are not part of the writing, but \
mention them in `notes` if they matter.
- In `question`, join the corrected body lines into one paragraph. Do not add anything the \
writer did not say.

You get two images: the full scanned page (for context and layout) and an enlarged crop of \
the handwriting (read from this one). If the crop seems to be missing part of the writing, \
use the full page for that part."""


class Correction(BaseModel):
    line: int = Field(description="Line number in `lines`, starting at 1.")
    as_written: str = Field(description="The word exactly as the writer spelled it.")
    corrected: str = Field(description="The correct spelling used in `lines`.")


class PageTranscription(BaseModel):
    lines: list[str] = Field(
        description="Transcription, one entry per handwritten line, in the same order and with the "
        "same line breaks as the paper. Spelling mistakes are corrected (each one listed in "
        "`corrections`); wording is otherwise unchanged. Wrap any word you are not sure about like ⟦word?⟧."
    )
    corrections: list[Correction] = Field(description="Every spelling mistake you corrected in `lines`. Empty if none.")
    greeting: str = Field(description="Opening salutation, e.g. กราบนมัสการพระอาจารย์... Empty if none.")
    question: str = Field(description="The body of the question as one clean paragraph: the corrected lines "
                          "joined, without the greeting and closing.")
    closing: str = Field(description="Closing/thanks line, e.g. ขอบพระคุณเจ้าค่ะ. Empty if none.")
    continues_from_previous: bool = Field(description="True if this page looks like the 2nd+ page of a question (e.g. numbered ②).")
    continues_to_next: bool = Field(description="True if the question clearly continues on the next page (arrow, numbering).")
    notes: str = Field(description="Anything a reviewer should know: underlines, crossed-out words, non-Thai words, "
                       "drawings, illegible parts.")
    confidence: Literal["high", "medium", "low"]


# --- Page images ---

def image_path(document_id: int, page_number: int, kind: str = "crop") -> str:
    suffix = "_crop" if kind == "crop" else ""
    return os.path.join(QNA_DIR, str(document_id), f"page_{page_number:03d}{suffix}.jpg")


def crop_to_handwriting(page: Image.Image) -> Image.Image:
    gray = np.asarray(page.convert("L"))
    height, width = gray.shape
    ink = gray < INK_DARKNESS

    ey, ex = int(height * EDGE_TOP_BOTTOM), int(width * EDGE_SIDES)
    ink[:ey, :] = ink[-ey:, :] = False
    ink[:, :ex] = ink[:, -ex:] = False
    # Scanner edges and paper shadows are long straight lines; handwriting never
    # fills a quarter of a whole column or row.
    ink[:, ink.mean(axis=0) > 0.25] = False
    ink[ink.mean(axis=1) > 0.25, :] = False

    # A row/column needs a few ink pixels to count, so dust specks don't widen the crop.
    rows = np.where(ink.sum(axis=1) > 3)[0]
    cols = np.where(ink.sum(axis=0) > 3)[0]
    if len(rows) == 0 or len(cols) == 0:
        return page

    crop = page.crop((max(cols[0] - CROP_PADDING, 0), max(rows[0] - CROP_PADDING, 0),
                      min(cols[-1] + CROP_PADDING, width), min(rows[-1] + CROP_PADDING, height)))
    scale = MAX_CROP_SIDE / max(crop.size)
    if scale > 1:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
    return crop


def render_pages(pdf_path: str, document_id: int) -> int:
    """Writes the full-page and cropped image for every page; returns the page count."""
    os.makedirs(os.path.join(QNA_DIR, str(document_id)), exist_ok=True)
    doc = fitz.open(pdf_path)
    for index, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=RENDER_DPI)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        image.save(image_path(document_id, index, "full"), quality=90)
        crop_to_handwriting(image).save(image_path(document_id, index, "crop"), quality=92)
    total = len(doc)
    doc.close()
    return total


def pdf_page_count(pdf_path: str) -> int:
    with fitz.open(pdf_path) as doc:
        return len(doc)


# --- Transcription ---

def _image_block(path: str, max_side: int | None = None) -> dict:
    image = Image.open(path).convert("RGB")
    if max_side:
        image.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buffer.getvalue()).decode()}}


def _call_claude(full_page: dict, crop: dict, api_key: str | None) -> dict:
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        # The instructions are identical for every page, so they're cached: an
        # explicit breakpoint at the end of the shared part, not top-level
        # automatic caching, which would put the breakpoint on this page's
        # images - unique per request, so every call would pay the cache-write
        # premium on them and nothing would ever be read back.
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "high"},  # careful reading matters more than speed here
        # If a page is ever declined by mistake, the API retries it on another model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "Full scanned page:"}, full_page,
            {"type": "text", "text": "Enlarged crop of the handwriting:"}, crop,
            {"type": "text", "text": "Transcribe this page."},
        ]}],
        output_format=PageTranscription,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"no transcription returned (stop_reason={response.stop_reason})")
    return {**response.parsed_output.model_dump(),
            "_meta": {"model": response.model, "input_tokens": response.usage.input_tokens,
                      "output_tokens": response.usage.output_tokens,
                      "cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
                      "cache_read_input_tokens": response.usage.cache_read_input_tokens}}


def to_fields(result: dict) -> dict:
    """Claude's structured answer -> the four plain-text fields a reviewer edits."""
    notes = [result.get("notes", "").strip()]
    if result.get("continues_from_previous"):
        notes.append("Continues from the previous page.")
    if result.get("continues_to_next"):
        notes.append("Continues on the next page.")
    return {
        "confidence": result.get("confidence", ""),
        "text": "\n".join(result.get("lines", [])),
        "clean": " ".join(filter(None, [result.get("greeting"), result.get("question"), result.get("closing")])),
        "fixes": "\n".join(f"line {c['line']}: {c['as_written']} -> {c['corrected']}"
                           for c in result.get("corrections", [])),
        "notes": " ".join(filter(None, notes)),
    }


def unsure_count(text: str) -> int:
    return len(UNSURE_PATTERN.findall(text))


def transcribe_document(document_id: int, api_key: str | None, log=print) -> tuple[int, int]:
    """Transcribes every pending/failed page of a document, CONCURRENT_PAGES at
    a time, saving each page as soon as it's done (so a reviewer can start on
    early pages while later ones are still running, and an interrupted run
    loses nothing already finished). Returns (succeeded, failed)."""
    conn = db.get_connection()
    try:
        pages = db.qna_pages_to_transcribe(conn, document_id)
    finally:
        conn.close()

    def work(page_number: int) -> bool:
        conn = db.get_connection()  # own connection - sqlite3 connections aren't shared across threads
        try:
            result = _call_claude(
                _image_block(image_path(document_id, page_number, "full"), FULL_PAGE_MAX_SIDE),
                _image_block(image_path(document_id, page_number, "crop")), api_key)
            fields = to_fields(result)
            db.save_qna_transcription(conn, document_id, page_number, ai_json=json.dumps(result, ensure_ascii=False),
                                      **fields)
            log(f"Page {page_number}: {fields['confidence']} confidence, {unsure_count(fields['text'])} unsure word(s)")
            return True
        except (anthropic.APIError, RuntimeError) as e:
            db.set_qna_page_error(conn, document_id, page_number, str(e))
            log(f"Page {page_number}: FAILED - {e}")
            return False
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=CONCURRENT_PAGES) as pool:
        results = list(pool.map(work, pages))
    return sum(results), len(results) - sum(results)


# --- Export ---

def _export_rows(conn, document_id: int, done_only: bool) -> list[dict]:
    pages = db.list_qna_pages(conn, document_id)
    return [p for p in pages if p["status"] in (("done",) if done_only else ("review", "done"))]


def export_csv(conn, document_id: int, done_only: bool = False) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["page", "status", "confidence", "clean", "text", "spelling_fixes", "notes"])
    for p in _export_rows(conn, document_id, done_only):
        writer.writerow([p["page_number"], p["status"], p["confidence"], p["clean"], p["text"],
                         p["fixes"].replace("\n", "; "), p["notes"]])
    # utf-8-sig: the BOM is what makes Excel open Thai text correctly instead of garbled.
    return buffer.getvalue().encode("utf-8-sig")


def export_docx(conn, document_id: int, done_only: bool = False) -> bytes:
    document = db.get_qna_document(conn, document_id)
    word = Document()
    word.styles["Normal"].font.size = Pt(14)
    word.add_heading(document["source_filename"], level=0)
    for p in _export_rows(conn, document_id, done_only):
        word.add_heading(f"Page {p['page_number']}  ({p['status']})", level=1)
        table = word.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        image_cell, text_cell = table.rows[0].cells
        image_cell.paragraphs[0].add_run().add_picture(image_path(document_id, p["page_number"]), width=Cm(8))
        text_cell.paragraphs[0].add_run(p["text"])
        if p["clean"]:
            word.add_paragraph().add_run("Clean version: ").bold = True
            word.add_paragraph(p["clean"])
        if p["fixes"]:
            word.add_paragraph("Spelling fixed (as written -> corrected): " + p["fixes"].replace("\n", "; "))
        if p["notes"]:
            word.add_paragraph(f"Notes: {p['notes']}")
        word.add_page_break()
    buffer = io.BytesIO()
    word.save(buffer)
    return buffer.getvalue()
