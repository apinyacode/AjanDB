"""
Chatbot-style "compile a book" feature: takes a free-text instruction,
retrieves matching documents from the store via keyword search, and asks
Claude to synthesise them into a coherent book.

Retrieval is a simple keyword search (see _extract_keywords / db.search),
not semantic search - good enough for "find files that mention this topic"
at small personal-library scale, and it keeps the store to plain SQLite
with no embeddings/vector index to run. The actual synthesis work (turning
scattered notes into an organised book) is left entirely to the model call,
which is what it's actually good at.

Two providers, same prompt/output contract - pick whichever API key you
actually have:
  - "anthropic" (default): claude-sonnet-5. Needs ANTHROPIC_API_KEY.
  - "openai": gpt-4o. Needs OPENAI_API_KEY.
Neither key is the same thing as a claude.ai or ChatGPT Plus subscription -
both are separate, billed-by-usage API credentials from each provider's own
developer console. The test suite mocks both clients, consistent with the
rest of this repo's approach to code that needs a key this environment
doesn't have.
"""
import os
import re

from anthropic import Anthropic
from openai import OpenAI

from . import db as db_module

# Embedded images (see convert.py) are markdown image links to a real .jpg
# file - meaningless to an LLM as prose either way, so swapped for a short
# bracketed label before anything is sent. (Matches both the current
# /images/<hash>.jpg links and the old inline base64 data URIs, in case a
# database still has rows saved before that changed.)
_IMAGE_MARKDOWN_RE = re.compile(r"!\[([^\]]*)\]\((?:/images/[^)]*|data:image/[^)]*)\)")


def _strip_embedded_images(markdown: str) -> str:
    return _IMAGE_MARKDOWN_RE.sub(lambda m: f"[{m.group(1) or 'image'}]", markdown)

DEFAULT_PROVIDER = "anthropic"

DEFAULT_MODEL_BY_PROVIDER = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
}

_API_KEY_ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_PROVIDER_DISPLAY_NAME = {"anthropic": "Anthropic", "openai": "OpenAI"}

SYSTEM_PROMPT = """You are compiling a book from a personal knowledge base of \
uploaded documents. You will be given the user's instruction for what the \
book should be about, followed by the full text of every source document \
that matched their topic.

Write a coherent, well-organised book in Markdown:
- Give it a title (# heading) and organise the content into logical chapters \
(## headings).
- Synthesise and reorganise the source material around the topic - don't just \
concatenate the documents verbatim.
- Preserve the substance and any direct guidance worth keeping, but cut \
tangents that don't serve the stated topic.
- If sources disagree, or a claim seems specific to just one source, say so \
briefly rather than presenting it as universal.
- If none of the provided source material is actually relevant to the \
instruction, say so plainly instead of inventing content.
"""

_STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "to", "on", "in", "about",
    "from", "with", "compile", "create", "make", "write", "book", "please",
    "me", "my", "all", "files", "content", "containing", "that", "into",
    "give", "find", "show",
}


def _extract_keywords(instruction: str) -> str:
    """Drops common instruction/stopwords so the search captures topic words
    ('child', 'raise') rather than noise words ('compile', 'about'). This is
    a retrieval heuristic, not language understanding - the actual synthesis
    happens in the model call below."""
    words = [w.strip(".,!?").lower() for w in instruction.split()]
    keywords = [w for w in words if w and w not in _STOPWORDS]
    return " ".join(keywords) if keywords else instruction


def _call_anthropic(user_message: str, model: str, api_key: str) -> str:
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def _call_openai(user_message: str, model: str, api_key: str) -> str:
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=8192,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content


_CALLERS = {"anthropic": _call_anthropic, "openai": _call_openai}


def compile_book(instruction: str, conn, provider: str = None, model: str = None,
                  api_key: str = None, max_sources: int = 20,
                  max_chars_per_source: int = 8000) -> dict:
    """Returns {"markdown": str, "sources": [filename, ...]}. `provider` picks
    which API this calls ("anthropic" or "openai", default "anthropic" or
    $LLM_PROVIDER); `model` defaults to that provider's flagship model."""
    provider = (provider or os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    if provider not in _CALLERS:
        raise ValueError(f"Unknown LLM provider {provider!r} - expected 'anthropic' or 'openai'")
    model = model or DEFAULT_MODEL_BY_PROVIDER[provider]

    query = _extract_keywords(instruction)
    matches = db_module.search(conn, query, limit=max_sources)

    if not matches:
        return {
            "markdown": (
                "# No matching source material found\n\n"
                "No uploaded documents matched this topic. Try uploading "
                "relevant files first, or rephrase the instruction."
            ),
            "sources": [],
        }

    env_var = _API_KEY_ENV_VAR[provider]
    resolved_key = api_key or os.environ.get(env_var)
    if not resolved_key:
        raise RuntimeError(
            f"{env_var} is not set. The book-compiling chatbot needs your own "
            f"{_PROVIDER_DISPLAY_NAME[provider]} API key to synthesise a book from the matched "
            f"sources - set it in the environment the backend runs in. This is "
            f"not the same as a claude.ai or ChatGPT Plus subscription."
        )

    source_blocks = []
    source_labels = []
    for m in matches:
        full = db_module.get_document(conn, m["id"])
        content = _strip_embedded_images(full["markdown"])[:max_chars_per_source]
        label = f"{full['source_filename']} (page {full['page_number']}/{full['total_pages']})"
        source_labels.append(label)
        source_blocks.append(f"--- SOURCE: {label} ---\n{content}")

    user_message = (
        f"Instruction: {instruction}\n\n"
        f"Source documents ({len(matches)} matched):\n\n" + "\n\n".join(source_blocks)
    )

    markdown = _CALLERS[provider](user_message, model, resolved_key)

    return {"markdown": markdown, "sources": source_labels}
