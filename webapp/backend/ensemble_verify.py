"""
Shared word-level text diffing, and the proactive **ensemble verification**
feature built on top of it.

review.py already had a manual, opt-in "Verify with second model" button:
a one-off cross-check of the page currently being reviewed against a
second vision-LLM provider, showing a word-level diff and an agreement
percentage. This module generalises that into something proactive: when a
reviewer turns on ensemble mode for a session, every page that actually
needs OCR/vision transcription (anything that isn't native, confidence-100
PDF text) is transcribed independently by *two* vision-LLM providers up
front, and the disagreement spans between them - not either model's own
uncalibrated, self-reported confidence score - become the primary signal
for what to look at first. Full agreement between two independently-run
models is a much stronger trust signal than one model being sure of
itself; see review.py's _run_ensemble_verification for how this plugs into
the review flow (gating, API-key resolution, the bounded-concurrency
timeout, and why a vision-engine session reuses its own already-converted
markdown as one of the two ensemble members instead of always making two
fresh calls).

word_diff() is the same word-tokenize-then-difflib approach review.py's
cross-checks and manual "verify with second model" used before this
module existed - moved here as the one shared implementation (review.py
imports it) rather than each caller keeping its own copy.
"""
import difflib
import re

from . import convert

_WORD_TOKEN_RE = re.compile(r"\S+|\s+")

DEFAULT_ENSEMBLE_PROVIDERS = ("anthropic", "openai")


def word_diff(a: str, b: str) -> dict:
    """Word-level diff between two transcriptions of the same page/span -
    lets a human see exactly which words two texts disagree on, instead of
    trusting either one's own confidence score alone. Tokenizes on
    whitespace boundaries rather than characters, so a single retyped word
    shows as one change, not a scattering of single-character ones.

    Returns {"segments": [...], "agreement_ratio": 0-100}. Each segment is
    {"tag": "equal", "text": ...} where both versions agree, or
    {"tag": "replace"|"delete"|"insert", "a": ..., "b": ...} where they
    differ ("a" is the first text, "b" the second)."""
    a_tokens = _WORD_TOKEN_RE.findall(a)
    b_tokens = _WORD_TOKEN_RE.findall(b)
    matcher = difflib.SequenceMatcher(None, a_tokens, b_tokens, autojunk=False)
    segments = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segments.append({"tag": "equal", "text": "".join(a_tokens[i1:i2])})
        else:
            segments.append({"tag": tag, "a": "".join(a_tokens[i1:i2]), "b": "".join(b_tokens[j1:j2])})
    return {"segments": segments, "agreement_ratio": round(matcher.ratio() * 100, 1)}


def convert_page_ensemble(pdf_path: str, providers: tuple[str, ...] = DEFAULT_ENSEMBLE_PROVIDERS,
                           api_keys: dict[str, str] | None = None) -> dict[str, str]:
    """Transcribes the single-page PDF at `pdf_path` independently with
    each provider in `providers`, returning {provider: markdown}. Reuses
    convert.pdf_to_markdown_vision (which itself wraps vision_ocr.py's
    per-provider calling functions via the pipeline's build_doc_items_vision)
    rather than duplicating any provider-calling logic - see this module's
    docstring for why that's the right level to call into rather than
    vision_ocr.py's raw per-page function directly: it's the same call
    review.py's existing manual "verify with second model" and automatic
    cross-provider check already make, so the output is directly
    comparable (same markdown assembly, same image-embedding behaviour) to
    what a reviewer already sees elsewhere in this app.

    `api_keys` maps provider name to the key to use for it; a provider not
    present there falls back to that provider's own environment variable
    (see vision_ocr.py). Raises whatever convert.pdf_to_markdown_vision
    raises (RuntimeError for a missing key, etc.) - callers decide whether
    to skip or propagate."""
    api_keys = api_keys or {}
    outputs = {}
    for provider in providers:
        chunks = convert.pdf_to_markdown_vision(pdf_path, provider=provider, api_key=api_keys.get(provider))
        outputs[provider] = chunks[0]["markdown"]
    return outputs


def diff_ensemble(outputs: dict[str, str]) -> dict:
    """Pairwise word-level diff between the (in practice, always exactly
    two) provider outputs in `outputs`, built on word_diff() above rather
    than a second diff implementation. Returns {"spans": [...],
    "agreement_pct": float}. Each span is either {"text", "agreement":
    "full"} for a stretch both providers produced identically, or
    {"providers": {name: text, ...}, "agreement": "none"} where they
    disagree - `providers` preserves `outputs`' own key order, so a caller
    that inserted its "primary"/canonical provider first (see review.py)
    can always find that provider's own wording at providers[Object.keys()[0]]
    (or, in Python, next(iter(...))) for e.g. highlighting purposes.

    Fewer than two providers has nothing to diff - one provider's own
    output is trivially "full agreement" with itself; no providers at all
    is an empty page."""
    names = list(outputs)
    if not names:
        return {"spans": [], "agreement_pct": 100.0}
    if len(names) == 1:
        text = outputs[names[0]]
        return {"spans": ([{"text": text, "agreement": "full"}] if text else []), "agreement_pct": 100.0}

    a_name, b_name = names[0], names[1]
    diff = word_diff(outputs[a_name], outputs[b_name])
    spans = []
    for seg in diff["segments"]:
        if seg["tag"] == "equal":
            spans.append({"text": seg["text"], "agreement": "full"})
        else:
            spans.append({"providers": {a_name: seg["a"], b_name: seg["b"]}, "agreement": "none"})
    return {"spans": spans, "agreement_pct": diff["agreement_ratio"]}
