"""
Word (.docx) export of a stored book - the "finished" counterpart to Data
Search's Export .md, only offered once a book is complete, has no page left
flagged "Needs review", and has been marked ready for publish (see main.py's
export_book_docx).

Converts each page's stored markdown (see convert.py) rather than re-running
the pipeline's own .docx assembler (pdf_to_docx_pipeline/pipeline/assemble.py):
that works from the pipeline's raw doc_items, which aren't kept, while the
markdown is exactly what a reviewer approved and may have corrected by hand.
Only the markdown convert.py and a human editor actually produce is handled -
headings, paragraphs (line breaks inside one kept), bullet lists, **bold** /
*italic*, and embedded page images (`![alt](/images/<hash>.jpg)`); anything
else comes through as plain text, never dropped.
"""
import io
import os
import re

from docx import Document
from docx.enum.text import WD_BREAK
from docx.shared import Cm, Pt

from . import convert

MAX_IMAGE_WIDTH = Cm(15)  # fits an A4/Letter page inside default margins

_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\((/images/[^)]+)\)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
_INLINE_RE = re.compile(r"(\*\*[^*\n]+\*\*|\*[^*\n]+\*)")


def _add_inline(paragraph, text: str):
    for part in _INLINE_RE.split(text):
        if len(part) > 4 and part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif len(part) > 2 and part.startswith("*") and part.endswith("*"):
            paragraph.add_run(part[1:-1]).italic = True
        elif part:
            paragraph.add_run(part)


def _add_image(document, alt: str, url: str):
    path = os.path.join(convert.IMAGES_DIR, os.path.basename(url))
    if not os.path.isfile(path):
        document.add_paragraph().add_run(f"[{alt or 'image'} - file missing]").italic = True
        return
    picture = document.add_paragraph().add_run().add_picture(path)
    if picture.width > MAX_IMAGE_WIDTH:
        picture.height = int(picture.height * MAX_IMAGE_WIDTH / picture.width)
        picture.width = MAX_IMAGE_WIDTH


def _add_markdown(document, markdown: str):
    for block in re.split(r"\n\s*\n", markdown.strip()):
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if len(lines) == 1 and _IMAGE_RE.match(lines[0]):
            _add_image(document, *_IMAGE_RE.match(lines[0]).groups())
        elif len(lines) == 1 and _HEADING_RE.match(lines[0]):
            hashes, text = _HEADING_RE.match(lines[0]).groups()
            document.add_heading(text, level=min(len(hashes) + 1, 9))  # level 1 is the page heading
        elif all(_BULLET_RE.match(line) for line in lines):
            for line in lines:
                _add_inline(document.add_paragraph(style="List Bullet"), _BULLET_RE.match(line).group(1))
        else:
            paragraph = document.add_paragraph()
            for i, line in enumerate(lines):
                if i:
                    paragraph.add_run().add_break(WD_BREAK.LINE)
                _add_inline(paragraph, line)


def book_to_docx(chunks: list[dict]) -> bytes:
    """`chunks`: one book's pages in order, as db.get_book_chunks returns them."""
    first = chunks[0]
    document = Document()
    document.styles["Normal"].font.size = Pt(12)
    document.add_heading(os.path.splitext(first["source_filename"])[0], level=0)
    document.add_paragraph().add_run(f"Category: {first['category']} · {first['total_pages']} page(s)").italic = True
    for index, chunk in enumerate(chunks):
        if index:
            document.add_page_break()
        document.add_heading(f"Page {chunk['page_number']}", level=1)
        _add_markdown(document, chunk["markdown"])
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
