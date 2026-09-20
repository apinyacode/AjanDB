"""
"Data Generation" text modes: takes a free-text instruction, retrieves
matching documents from the store via keyword search, and either lists
them verbatim (`compile_copy_paste` - no model call at all) or asks an LLM
to build new material *around* them (`compile_book` - see its `mode`
argument for the three flavors of that). content_studio.py's image/video
modes are built on top of this module's retrieval and `compile_book`
(for a video's narration script), rather than duplicating either.

Retrieval is a simple keyword search (see _extract_keywords / db.search),
not semantic search - good enough for "find files that mention this topic"
at small personal-library scale, and it keeps the store to plain SQLite
with no embeddings/vector index to run. The actual generative work is left
entirely to the model call, which is what it's actually good at.

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

# Every "generative" text mode shares the same rule that makes it
# "generative" rather than "rewritten": never alter the source material's
# own wording, only add new material around it. Each mode below just
# targets a different shape of output around that same constraint.

_TEXT_SYSTEM_PROMPT = """You are creating new material from a personal knowledge base of \
uploaded documents, for the "Generative" mode of a content-creation tool. You will be \
given the user's instruction for what to create, followed by the full text of every \
source document that matched their topic.

Write new material in Markdown that is built *around* the source material rather than a \
rewrite of it:
- Never reword, paraphrase, or alter the original wording of the source text. Where you \
draw on a source directly, quote it exactly (e.g. in a blockquote), and cite which source \
it came from right next to the quote.
- Add new connective, explanatory, and contextual prose of your own around those quotes - \
framing, transitions, synthesis, analysis - so the result reads as one coherent new piece, \
not a patchwork of quotes.
- If none of the provided source material is actually relevant to the instruction, say so \
plainly instead of inventing content.
"""

_FLOW_SYSTEM_PROMPT = """You are creating new material from a personal knowledge base of \
uploaded documents, for the "Generative - link across books" mode of a content-creation \
tool. You will be given the user's instruction for what to create, followed by the full \
text of every source document that matched their topic - drawn from what may be several \
different books.

Identify passages that address the same topic across *different* source books, and weave \
them into one continuous narrative flow that moves between books:
- Never reword, paraphrase, or alter the original wording of the source text. Quote each \
passage exactly (e.g. in a blockquote), and cite which specific book it came from right \
next to the quote.
- Write your own connective narration between quotes to carry the reader from one book's \
treatment of the topic to the next, pointing out where they agree, disagree, or build on \
each other.
- If none of the provided source material is actually relevant to the instruction, say so \
plainly instead of inventing content.
"""

_VIDEO_SCRIPT_SYSTEM_PROMPT = """You are writing a short narration script for a vertical \
short-form video (TikTok-length, about 45-75 seconds spoken aloud - roughly 120-180 \
words), from a personal knowledge base of uploaded documents. You will be given the \
user's instruction for what the video should be about, followed by the full text of \
every source document that matched their topic.

Write the script as plain narration text only - no markdown headers, no stage directions, \
nothing but words meant to be spoken aloud:
- Never reword, paraphrase, or alter the original wording of the source text. Where you \
draw on a source directly, speak it verbatim, introduced naturally (e.g. "As one book puts \
it, ...").
- Add your own short connective narration around those quotes so the whole thing flows as \
one short, coherent spoken piece.
- Keep it tight - this is a short clip, not a full essay.
- If none of the provided source material is actually relevant to the instruction, say so \
plainly instead of inventing content.
"""

_SYSTEM_PROMPTS = {
    "text": _TEXT_SYSTEM_PROMPT,
    "flow": _FLOW_SYSTEM_PROMPT,
    "video_script": _VIDEO_SCRIPT_SYSTEM_PROMPT,
}

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


def retrieve_matches(instruction: str, conn, max_sources: int = 20) -> list[dict]:
    """Keyword-searches the library for `instruction`'s topic and returns
    the full matched documents (not just the search index's own row shape),
    each annotated with a human-readable `label` ("filename (page X/Y)") -
    shared by every Data Generation mode: this module's own copy-paste and
    generative text/flow/video-script modes, plus content_studio.py's
    image/video modes."""
    query = _extract_keywords(instruction)
    matches = db_module.search(conn, query, limit=max_sources)
    docs = []
    for m in matches:
        full = dict(db_module.get_document(conn, m["id"]))
        full["label"] = f"{full['source_filename']} (page {full['page_number']}/{full['total_pages']})"
        docs.append(full)
    return docs


def compile_copy_paste(instruction: str, conn, max_sources: int = 20) -> dict:
    """Returns {"markdown": str, "sources": [label, ...]} - pure retrieval,
    no model call: lists the exact stored content of every matching page
    verbatim, each clearly cited by source file/page. For when the goal is
    "show me what the books actually say," not a generated piece built
    around them (see compile_book for that)."""
    docs = retrieve_matches(instruction, conn, max_sources)
    if not docs:
        return {
            "markdown": (
                "# No matching source material found\n\n"
                "No uploaded documents matched this topic. Try uploading "
                "relevant files first, or rephrase the instruction."
            ),
            "sources": [],
        }
    blocks = [f"## {doc['label']}\n\n{doc['markdown']}" for doc in docs]
    markdown = f"# Source material matching: {instruction}\n\n" + "\n\n---\n\n".join(blocks)
    return {"markdown": markdown, "sources": [doc["label"] for doc in docs]}


def _call_anthropic(user_message: str, model: str, api_key: str, system_prompt: str) -> str:
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def _call_openai(user_message: str, model: str, api_key: str, system_prompt: str) -> str:
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=8192,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content


_CALLERS = {"anthropic": _call_anthropic, "openai": _call_openai}


def compile_book(instruction: str, conn, provider: str = None, model: str = None,
                  api_key: str = None, mode: str = "text", max_sources: int = 20,
                  max_chars_per_source: int = 8000) -> dict:
    """Returns {"markdown": str, "sources": [filename, ...]}. `provider` picks
    which API this calls ("anthropic" or "openai", default "anthropic" or
    $LLM_PROVIDER); `model` defaults to that provider's flagship model.

    `mode` picks which of _SYSTEM_PROMPTS drives the generation: "text"
    (default, plain generative synthesis), "flow" (explicitly links the
    same topic across different source books into one narrative), or
    "video_script" (a short TikTok-length narration script for
    content_studio.py's video mode)."""
    if mode not in _SYSTEM_PROMPTS:
        raise ValueError(f"Unknown compile_book mode {mode!r} - expected one of {sorted(_SYSTEM_PROMPTS)}")
    provider = (provider or os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    if provider not in _CALLERS:
        raise ValueError(f"Unknown LLM provider {provider!r} - expected 'anthropic' or 'openai'")
    model = model or DEFAULT_MODEL_BY_PROVIDER[provider]

    docs = retrieve_matches(instruction, conn, max_sources)

    if not docs:
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
    for doc in docs:
        content = _strip_embedded_images(doc["markdown"])[:max_chars_per_source]
        source_labels.append(doc["label"])
        source_blocks.append(f"--- SOURCE: {doc['label']} ---\n{content}")

    user_message = (
        f"Instruction: {instruction}\n\n"
        f"Source documents ({len(docs)} matched):\n\n" + "\n\n".join(source_blocks)
    )

    markdown = _CALLERS[provider](user_message, model, resolved_key, _SYSTEM_PROMPTS[mode])

    return {"markdown": markdown, "sources": source_labels}
