"""
Converts an uploaded "unstructured file" into markdown *chunks* for storage,
each no bigger than roughly one page.

PDFs are routed through the existing pdf_to_docx_pipeline (born-digital text
extraction, local OCR or vision-LLM, handwriting detection) - this module
renders that pipeline's content items as one chunk per PDF page instead of
a .docx, carrying an OCR confidence score and a human-review flag per chunk.
Plain text/markdown and .docx files have no OCR step, so they're chunked by
a rough character budget instead, with confidence left unset (nothing was
guessed) and needs_review always False.

Every chunk is a dict: {"page_number", "markdown", "confidence", "needs_review"}.
`confidence` is 0-100 or None (not applicable - native text, no OCR involved).
"""
import os
import re
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PIPELINE_DIR = os.path.join(_REPO_ROOT, "pdf_to_docx_pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from pipeline import ocr as pdf_ocr  # noqa: E402
from pipeline.main import build_doc_items  # noqa: E402
from pipeline.main_vision import build_doc_items_vision  # noqa: E402

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")

# A chunk's average OCR/transcription confidence below this triggers
# needs_review, on top of any handwriting/uncertain region flagged outright.
CONFIDENCE_REVIEW_THRESHOLD = 75.0

# Rough per-chunk size cap for non-paginated sources (.txt/.md/.docx), where
# "one page" isn't a concept the file format gives us - chosen to read like
# a page of prose, not to hit any hard limit.
PLAIN_TEXT_CHUNK_CHARS = 3000

# Same class of artifact pipeline.assemble strips before writing .docx (stray
# control characters, e.g. from a font missing a glyph during PDF text
# extraction) - needs stripping here too since this is a second consumer of
# the same extracted text blocks.
_CONTROL_CHARS_RE = re.compile(
    "[" + "".join(chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)) + "]"
)


def _clean(text: str) -> str:
    return _CONTROL_CHARS_RE.sub("", text)


_SUMMARY_LABELS = {
    "image": "image(s)",
    "handwriting": "handwritten/low-confidence region(s)",
    "uncertain": "low-confidence OCR region(s)",
}
_FLAGGED_KINDS = ("image", "handwriting", "uncertain")
# Of the flagged kinds, only these represent content that genuinely couldn't
# be transcribed reliably - a plain photo isn't itself a review-worthy
# accuracy problem, so it's reported in the summary but doesn't flag the page.
_REVIEW_TRIGGER_KINDS = ("handwriting", "uncertain")


def items_to_chunks(doc_items: list[dict]) -> list[dict]:
    """Turns one PDF's ordered content items into one chunk per page. Non-text
    items (images, handwriting, low-confidence regions) aren't inlined - a
    scanned page in a script with heavy diacritics (Thai, Vietnamese, ...)
    can produce dozens of tiny mis-split OCR fragments that the handwriting
    heuristic flags individually (see pipeline/handwriting.py's docstring),
    so these are counted and rolled into one summary line per page instead
    of a placeholder per item."""
    chunks = []
    state = {"page": None, "lines": [], "confidences": [], "counts": {k: 0 for k in _FLAGGED_KINDS}}

    def flush():
        if state["page"] is None:
            return
        counts = state["counts"]
        summary_parts = [f"{counts[k]} {_SUMMARY_LABELS[k]}" for k in _FLAGGED_KINDS if counts[k]]
        lines = list(state["lines"])
        if summary_parts:
            lines.append("*[Not shown as text: " + ", ".join(summary_parts) + " - see the original document]*")
        markdown = "\n\n".join(lines).strip() or "*[No extractable text on this page]*"

        confidences = state["confidences"]
        avg_confidence = round(sum(confidences) / len(confidences), 1) if confidences else None
        needs_review = (
            any(counts[k] for k in _REVIEW_TRIGGER_KINDS)
            or (avg_confidence is not None and avg_confidence < CONFIDENCE_REVIEW_THRESHOLD)
        )
        chunks.append({
            "page_number": state["page"] + 1,
            "markdown": markdown,
            "confidence": avg_confidence,
            "needs_review": needs_review,
        })

    for item in doc_items:
        kind = item["kind"]
        if kind == "heading":
            flush()
            state = {"page": item["page"], "lines": [], "confidences": [], "counts": {k: 0 for k in _FLAGGED_KINDS}}
        elif kind == "text":
            text = _clean(item["text"]).strip()
            if text:
                state["lines"].append(text)
            confidence = item.get("confidence")
            if confidence is not None:
                state["confidences"].append(confidence)
        elif kind in state["counts"]:
            state["counts"][kind] += 1
    flush()
    return chunks


def _chunk_plain_text(text: str, max_chars: int = PLAIN_TEXT_CHUNK_CHARS) -> list[dict]:
    """Packs paragraphs into ~page-sized pieces for formats with no OCR step
    and no page concept of their own. Confidence is left unset (nothing was
    guessed - it's exactly what was in the file) and nothing is ever flagged
    for review."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    packed: list[str] = []
    current: list[str] = []
    current_len = 0
    for p in paragraphs:
        if current and current_len + len(p) > max_chars:
            packed.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(p)
        current_len += len(p)
    if current:
        packed.append("\n\n".join(current))
    if not packed:
        packed = [text.strip() or "*[Empty file]*"]
    return [
        {"page_number": i + 1, "markdown": chunk, "confidence": None, "needs_review": False}
        for i, chunk in enumerate(packed)
    ]


def pdf_to_markdown(pdf_path: str, langs: str = None) -> list[dict]:
    doc_items, _ = build_doc_items(pdf_path, langs=langs or pdf_ocr.DEFAULT_LANGS)
    return items_to_chunks(doc_items)


def pdf_to_markdown_vision(pdf_path: str, provider: str = None, model: str = None) -> list[dict]:
    """Same as pdf_to_markdown, but scanned pages go through a vision-LLM
    (Claude or GPT-4o, see pipeline/vision_ocr.py) instead of local
    Tesseract - better at messy real-world scans and non-Latin scripts, at
    the cost of a per-page API call. Needs ANTHROPIC_API_KEY or
    OPENAI_API_KEY set in the backend's environment, matching `provider`.
    Confidence per chunk is the model's own self-reported estimate, not a
    calibrated metric like Tesseract's - treat it as a rough signal."""
    doc_items, _ = build_doc_items_vision(pdf_path, provider=provider, model=model)
    return items_to_chunks(doc_items)


def docx_to_markdown(docx_path: str) -> list[dict]:
    from docx import Document

    doc = Document(docx_path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return _chunk_plain_text("\n\n".join(paragraphs))


def text_to_markdown(text_path: str) -> list[dict]:
    with open(text_path, "r", encoding="utf-8", errors="replace") as f:
        return _chunk_plain_text(f.read())


def convert_to_markdown(file_path: str, filename: str, engine: str = "classical",
                         provider: str = None, model: str = None) -> list[dict]:
    """`engine`: "classical" (default, free, local Tesseract) or "vision"
    (per-page LLM call via `provider`/`model` - see pdf_to_markdown_vision).
    Only affects PDFs; other file types have nothing to digitise. Returns a
    list of chunk dicts, each no bigger than roughly one page."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        if engine == "vision":
            return pdf_to_markdown_vision(file_path, provider=provider, model=model)
        if engine != "classical":
            raise ValueError(f"Unknown conversion engine {engine!r} - expected 'classical' or 'vision'")
        return pdf_to_markdown(file_path)
    if ext == ".docx":
        return docx_to_markdown(file_path)
    if ext in (".txt", ".md"):
        return text_to_markdown(file_path)
    raise ValueError(f"Unsupported file type: {ext or '(none)'}")
