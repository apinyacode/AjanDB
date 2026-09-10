"""
Converts an uploaded "unstructured file" into markdown text for storage.

PDFs are routed through the existing pdf_to_docx_pipeline (born-digital text
extraction, local OCR, handwriting detection) - this module just renders
that pipeline's content items as markdown instead of a .docx. Plain text/
markdown and .docx files are handled directly since there's no digitisation
work to do there.
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

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")

# Same class of artifact pipeline.assemble strips before writing .docx (stray
# control characters, e.g. from a font missing a glyph during PDF text
# extraction) - needs stripping here too since this is a second consumer of
# the same extracted text blocks.
_CONTROL_CHARS_RE = re.compile(
    "[" + "".join(chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)) + "]"
)


def _clean(text: str) -> str:
    return _CONTROL_CHARS_RE.sub("", text)


def items_to_markdown(doc_items: list[dict]) -> str:
    """Mirrors pipeline.assemble.build_docx's structure, but as markdown text.
    Images aren't inlined (there's nowhere to put the bytes in plain text) -
    each is replaced with a short note, since the point of this store is
    searchable/compilable text content, not a faithful visual reproduction."""
    lines = []
    for item in doc_items:
        kind = item["kind"]
        if kind == "heading":
            lines.append(f"## Page {item['page'] + 1}")
        elif kind == "text":
            text = _clean(item["text"]).strip()
            if text:
                lines.append(text)
        elif kind == "image":
            lines.append("*[Image omitted]*")
        elif kind == "handwriting":
            lines.append(
                "*[Handwritten note - kept as an image in the original, "
                "needs manual review/transcription]*"
            )
        elif kind == "uncertain":
            lines.append("*[Low-confidence OCR region omitted]*")
    return "\n\n".join(lines).strip() + "\n"


def pdf_to_markdown(pdf_path: str, langs: str = None) -> str:
    doc_items, _ = build_doc_items(pdf_path, langs=langs or pdf_ocr.DEFAULT_LANGS)
    return items_to_markdown(doc_items)


def docx_to_markdown(docx_path: str) -> str:
    from docx import Document

    doc = Document(docx_path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs) + "\n"


def text_to_markdown(text_path: str) -> str:
    with open(text_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def convert_to_markdown(file_path: str, filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return pdf_to_markdown(file_path)
    if ext == ".docx":
        return docx_to_markdown(file_path)
    if ext in (".txt", ".md"):
        return text_to_markdown(file_path)
    raise ValueError(f"Unsupported file type: {ext or '(none)'}")
