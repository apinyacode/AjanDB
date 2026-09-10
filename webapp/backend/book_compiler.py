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

Requires ANTHROPIC_API_KEY to be set for real use; the test suite mocks the
client, consistent with the rest of this repo's approach to code that needs
a key this environment doesn't have.
"""
import os

from anthropic import Anthropic

from . import db as db_module

DEFAULT_MODEL = "claude-sonnet-5"

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


def compile_book(instruction: str, conn, model: str = DEFAULT_MODEL,
                  api_key: str = None, max_sources: int = 20,
                  max_chars_per_source: int = 8000) -> dict:
    """Returns {"markdown": str, "sources": [filename, ...]}."""
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

    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not resolved_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. The book-compiling chatbot needs "
            "your own Anthropic API key to synthesise a book from the "
            "matched sources - set it in the environment the backend runs in."
        )

    source_blocks = []
    for m in matches:
        full = db_module.get_document(conn, m["id"])
        content = full["markdown"][:max_chars_per_source]
        source_blocks.append(f"--- SOURCE: {full['filename']} ---\n{content}")

    user_message = (
        f"Instruction: {instruction}\n\n"
        f"Source documents ({len(matches)} matched):\n\n" + "\n\n".join(source_blocks)
    )

    client = Anthropic(api_key=resolved_key)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    markdown = "".join(b.text for b in response.content if b.type == "text")

    return {"markdown": markdown, "sources": [m["filename"] for m in matches]}
