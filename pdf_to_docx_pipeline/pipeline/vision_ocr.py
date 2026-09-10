"""
Stage 2 (alternative engine): vision-LLM based extraction for scanned pages.

Why this exists alongside ocr.py: classical CV (Tesseract + geometry
heuristics) works well and costs almost nothing when a document is clean
printed text on a plain background. It struggles when a document mixes
dense non-Latin text with photos/halftone textures on the same page -
telling "paragraph" from "photo" by pixel statistics alone gets genuinely
unreliable, as shown in testing on a real scanned book. A vision-language
model doesn't have that problem: it understands the page semantically
rather than measuring stroke widths, so it can correctly separate text from
photos from handwriting even on messy real-world scans, and it reads
non-Latin scripts far more robustly than a small local OCR model.

The trade-off is explicit and real: this calls a cloud model per page,
which costs tokens and real energy, undoing the "minimal footprint" framing
of the classical pipeline. Use this deliberately, for documents where
ocr.py's output quality isn't good enough - not as the default for every
PDF. A sensible policy: try the classical pipeline first (main.py), and
only fall back to this one for pages/documents where it clearly fails.

Requires: pip install anthropic, and an ANTHROPIC_API_KEY environment
variable set to your own API key. Nothing in this file will work without
that key - Claude.ai's sandboxed environment does not have one.
"""
import base64
import json
import os
import re

from anthropic import Anthropic

DEFAULT_MODEL = "claude-sonnet-5"  # claude-haiku-4-5-20251001 is cheaper/faster
                                    # for straightforward pages; claude-opus-4-8
                                    # for the hardest handwriting-heavy pages.

PROMPT = """You are digitising a scanned book/document page for a Word document.

Return ONLY a JSON object (no markdown fences, no commentary) shaped like:
{
  "blocks": [
    {"type": "text", "language": "th", "text": "...", "bbox": [x0, y0, x1, y1]},
    {"type": "photo", "bbox": [x0, y0, x1, y1]},
    {"type": "handwriting", "text": "best-effort transcription, or null if illegible", "bbox": [x0, y0, x1, y1]}
  ]
}

Rules:
- List blocks in natural top-to-bottom reading order.
- "bbox" is [left, top, right, bottom] as FRACTIONS of the image width/height (0.0-1.0), tightly
  cropping just that block.
- "type": "text" for any printed/typed text (merge into natural paragraphs, don't break every line).
  "photo" for photographs, illustrations, decorative graphics - do not attempt to transcribe these,
  just give an accurate bbox. "handwriting" for handwritten notes/annotations - attempt a real
  transcription; if genuinely illegible, set "text" to null rather than guessing.
- "language" (text blocks only): best-guess language code (e.g. "th", "en"). If a block mixes
  languages, pick the dominant one.
- Transcribe text exactly as written, including any errors in the original - don't correct or
  modernise spelling.
- Do not skip page numbers, headers, or footers - include them as their own text blocks.
"""


def _encode_image(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode("ascii")


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


def extract_page_with_vision(png_bytes: bytes, model: str = DEFAULT_MODEL,
                              api_key: str = None) -> list[dict]:
    """Sends one rendered page image to a Claude vision model and returns a
    list of blocks, each with pixel-space bbox coordinates already converted
    from the model's normalized (0-1) output."""
    client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": _encode_image(png_bytes),
                }},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )

    raw = "".join(b.text for b in response.content if b.type == "text")
    parsed = json.loads(_strip_code_fences(raw))

    # need real pixel dimensions to convert the model's normalized bboxes
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png_bytes))
    w, h = img.size

    blocks = []
    for b in parsed.get("blocks", []):
        x0, y0, x1, y1 = b["bbox"]
        blocks.append({
            **b,
            "bbox_px": (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)),
        })
    return blocks
