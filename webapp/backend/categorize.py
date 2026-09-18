"""
Best-effort LLM category suggestion for a freshly-uploaded document.

Deliberately never blocks or fails the upload: no API key, a bad provider
name, a network error, or an empty response all just fall back to None, and
the caller defaults to "Uncategorized" - the label is a convenience the
uploader can always retype, not something worth failing a whole upload over.
"""
import os
import re

from anthropic import Anthropic
from openai import OpenAI

# Embedded images (see convert.py) are markdown image links to a real .jpg
# file - meaningless to an LLM as prose, so stripped before guessing a
# category from the excerpt. (Matches both the current /images/<hash>.jpg
# links and the old inline base64 data URIs, in case a database still has
# rows saved before that changed.)
_IMAGE_MARKDOWN_RE = re.compile(r"!\[([^\]]*)\]\((?:/images/[^)]*|data:image/[^)]*)\)")

DEFAULT_PROVIDER = "anthropic"

DEFAULT_MODEL_BY_PROVIDER = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
}

_API_KEY_ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

PROMPT_TEMPLATE = (
    "Reply with ONLY a short content-category label (2-4 words, Title Case) "
    "for the following document excerpt - e.g. \"Dharma Talks\", \"Recipes\", "
    "\"Meeting Notes\", \"Field Reports\". No punctuation, no quotes, no "
    "explanation - just the label.\n\n---\n{excerpt}"
)

_MAX_EXCERPT_CHARS = 2000
_MAX_LABEL_CHARS = 60


def _call_anthropic(prompt: str, model: str, api_key: str) -> str:
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model, max_tokens=20, messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in response.content if b.type == "text")


def _call_openai(prompt: str, model: str, api_key: str) -> str:
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model, max_tokens=20, messages=[{"role": "user", "content": prompt}])
    return response.choices[0].message.content


_CALLERS = {"anthropic": _call_anthropic, "openai": _call_openai}


def suggest_category(text: str, provider: str = None, model: str = None,
                      api_key: str = None) -> str | None:
    """Returns a short label, or None if suggestion isn't possible/failed -
    the caller should default to "Uncategorized" in that case."""
    excerpt = _IMAGE_MARKDOWN_RE.sub("", text or "").strip()[:_MAX_EXCERPT_CHARS]
    if not excerpt:
        return None

    provider = (provider or os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    if provider not in _CALLERS:
        return None
    model = model or DEFAULT_MODEL_BY_PROVIDER[provider]

    resolved_key = api_key or os.environ.get(_API_KEY_ENV_VAR[provider])
    if not resolved_key:
        return None

    prompt = PROMPT_TEMPLATE.format(excerpt=excerpt)
    try:
        label = _CALLERS[provider](prompt, model, resolved_key)
    except Exception:
        return None

    label = (label or "").strip().strip('"').strip("'").strip()
    return label[:_MAX_LABEL_CHARS] if label else None
