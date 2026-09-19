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

Two providers, same prompt/output contract - pick whichever API key you
actually have:
  - "anthropic" (default): claude-sonnet-5, or claude-opus-4-8 for the
    hardest handwriting-heavy/messy-scan pages. Needs ANTHROPIC_API_KEY.
  - "openai": gpt-4o. Needs OPENAI_API_KEY.
Neither key is the same thing as a claude.ai or ChatGPT Plus subscription -
both are separate, billed-by-usage API credentials from each provider's own
developer console.
"""
import base64
import json
import os
import re

from anthropic import Anthropic
from openai import OpenAI

DEFAULT_PROVIDER = "anthropic"

DEFAULT_MODEL_BY_PROVIDER = {
    "anthropic": "claude-sonnet-5",  # claude-haiku-4-5-20251001 is cheaper/faster
                                      # for straightforward pages; claude-opus-4-8
                                      # for the hardest handwriting-heavy pages.
    "openai": "gpt-4o",
}

PROMPT = """You are digitising a scanned book/document page for a Word document.

Return ONLY a JSON object (no markdown fences, no commentary) shaped like:
{
  "blocks": [
    {"type": "text", "language": "th", "text": "...", "bbox": [x0, y0, x1, y1], "confidence": 95},
    {"type": "photo", "bbox": [x0, y0, x1, y1]},
    {"type": "handwriting", "text": "best-effort transcription, or null if illegible", "bbox": [x0, y0, x1, y1], "confidence": 60}
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
- "confidence" (text and handwriting blocks only, omit for photo): your own honest 0-100 estimate
  of how certain you are the transcription is fully correct. 100 = certain and unambiguous, lower
  for unclear handwriting, damaged/blurry print, or guessed characters. Be genuinely self-critical -
  this drives a human-review flag downstream, so an overconfident score defeats its purpose.
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


# JSON escape characters that json.loads accepts after a backslash - anything
# else is a decode error, e.g. json.decoder.JSONDecodeError: Invalid \escape.
_VALID_JSON_ESCAPE_CHARS = '"\\/bfnrtu'
_INVALID_ESCAPE_RE = re.compile(r'\\(?![' + re.escape(_VALID_JSON_ESCAPE_CHARS) + '])')


def _repair_invalid_escapes(text: str) -> str:
    """Vision-LLM responses occasionally contain a literal backslash that
    isn't part of a valid JSON escape sequence - most often inside
    transcribed text that itself contains one (a fraction, a file path, a
    stray OCR artifact), which json.loads rejects outright rather than
    treating as a literal character the way a human reader would. Doubles
    any such backslash so it parses as a literal '\\', without touching
    already-valid escapes like \\n or \\"."""
    return _INVALID_ESCAPE_RE.sub(r"\\\\", text)


def _parse_model_json(raw: str) -> dict:
    cleaned = _strip_code_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_repair_invalid_escapes(cleaned))
    except json.JSONDecodeError as e:
        snippet = cleaned if len(cleaned) <= 300 else cleaned[:300] + "..."
        raise ValueError(
            f"Vision-LLM response wasn't valid JSON even after escape repair ({e}). "
            f"Response started with: {snippet!r}"
        ) from e


_API_KEY_ENV_VAR = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
_PROVIDER_DISPLAY_NAME = {"anthropic": "Anthropic", "openai": "OpenAI"}


def _call_anthropic(png_bytes: bytes, model: str, api_key: str) -> str:
    client = Anthropic(api_key=api_key)
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
    return "".join(b.text for b in response.content if b.type == "text")


def _call_openai(png_bytes: bytes, model: str, api_key: str) -> str:
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{_encode_image(png_bytes)}",
                }},
            ],
        }],
    )
    return response.choices[0].message.content


_CALLERS = {"anthropic": _call_anthropic, "openai": _call_openai}


def extract_page_with_vision(png_bytes: bytes, model: str = None, api_key: str = None,
                              provider: str = None) -> list[dict]:
    """Sends one rendered page image to a vision-LLM and returns a list of
    blocks, each with pixel-space bbox coordinates already converted from
    the model's normalized (0-1) output. `provider` picks which API this
    calls ("anthropic" or "openai", default "anthropic" or
    $VISION_LLM_PROVIDER); `model` defaults to that provider's flagship
    model if omitted."""
    provider = (provider or os.environ.get("VISION_LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    if provider not in _CALLERS:
        raise ValueError(f"Unknown vision LLM provider {provider!r} - expected 'anthropic' or 'openai'")
    model = model or DEFAULT_MODEL_BY_PROVIDER[provider]

    env_var = _API_KEY_ENV_VAR[provider]
    resolved_key = api_key or os.environ.get(env_var)
    if not resolved_key:
        raise RuntimeError(
            f"{env_var} is not set. The vision-LLM OCR engine needs your own "
            f"{_PROVIDER_DISPLAY_NAME[provider]} API key - this is not the same as a claude.ai "
            f"or ChatGPT Plus subscription."
        )

    raw = _CALLERS[provider](png_bytes, model, resolved_key)
    parsed = _parse_model_json(raw)
    return _blocks_with_pixel_bboxes(parsed, png_bytes)


def _blocks_with_pixel_bboxes(parsed: dict, png_bytes: bytes) -> list[dict]:
    """Converts each block's normalized (0-1) bbox to real pixel coordinates
    - shared by extract_page_with_vision above and
    extract_page_with_vision_logprobs below."""
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


def extract_page_with_vision_logprobs(png_bytes: bytes, model: str = None,
                                       api_key: str = None) -> tuple[list[dict], str, list[tuple[str, float]]]:
    """OpenAI (GPT-4o) only: same page-to-blocks extraction as
    extract_page_with_vision, but additionally requests per-token log-
    probabilities on the completion, for callers that want to flag spans
    the model was statistically unsure about - a signal orthogonal to (and
    less gameable than) its own self-reported confidence number, since a
    raw next-token probability isn't something the model can simply
    overstate the way a free-text self-assessment can. The Anthropic
    Messages API doesn't currently expose per-token logprobs, so there is
    no equivalent for that provider - callers check which provider handled
    a page before calling this (see review.py's _openai_logprob_flags).

    Returns (blocks, raw_completion_text, token_logprobs) where
    token_logprobs is [(token_string, logprob), ...] in completion order -
    concatenating every token_string reconstructs raw_completion_text
    exactly, which is what lets a caller map a token span back to a
    character range in it."""
    model = model or DEFAULT_MODEL_BY_PROVIDER["openai"]
    resolved_key = api_key or os.environ.get(_API_KEY_ENV_VAR["openai"])
    if not resolved_key:
        raise RuntimeError(
            f"{_API_KEY_ENV_VAR['openai']} is not set. The vision-LLM OCR engine needs your own "
            f"{_PROVIDER_DISPLAY_NAME['openai']} API key - this is not the same as a claude.ai "
            f"or ChatGPT Plus subscription."
        )

    client = OpenAI(api_key=resolved_key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        logprobs=True,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{_encode_image(png_bytes)}",
                }},
            ],
        }],
    )
    raw = response.choices[0].message.content
    choice_logprobs = response.choices[0].logprobs
    token_logprobs = (
        [(item.token, item.logprob) for item in choice_logprobs.content]
        if choice_logprobs and choice_logprobs.content else []
    )

    parsed = _parse_model_json(raw)
    blocks = _blocks_with_pixel_bboxes(parsed, png_bytes)
    return blocks, raw, token_logprobs
