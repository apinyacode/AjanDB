"""
Thai spell-check pass for the review UI's converted-text side: flags Thai
words that aren't in pythainlp's dictionary, so a reviewer sees exactly
which words in the OCR/vision-LLM output to double-check, with a suggested
correction alongside each one - the same "look here first" idea as the
low-confidence highlighting in convert.py, but for typos rather than
uncertain transcription.

Advisory only, like the confidence highlighting: a dictionary-based
spell-checker will also flag genuine words it simply doesn't know (proper
nouns, slang, loanwords), so this never blocks approval - it's a hint, not
a validator. Runs once against the page's converted markdown, same as
flagged_snippets; editing a flagged word away makes its highlight
disappear (see app.js's updateReviewHighlight) without re-running the
check.
"""
import re

from pythainlp.corpus import thai_words
from pythainlp.spell import spell
from pythainlp.tokenize import word_tokenize

_THAI_WORDS = thai_words()
_THAI_CHAR_RE = re.compile(r"[฀-๿]")
# Single Thai vowels/tone marks tokenize out on their own sometimes and are
# never worth a dictionary lookup by themselves.
_MIN_WORD_LEN = 2


def find_thai_typos(markdown: str) -> list[dict]:
    """Returns [{"word": ..., "suggestions": [...]}] for each distinct Thai
    word in `markdown` that isn't in pythainlp's dictionary, in first-seen
    order. `suggestions` is pythainlp's own ranked candidate list (possibly
    empty, if it has no guess at all).

    Uses the "longest" tokenizer engine rather than the default "newmm" -
    newmm's graph-based max-matching silently "fixes" a genuinely misspelled
    word by shattering it into whatever smaller in-dictionary syllables fit
    (e.g. "สวัสดร" -> "ส" + "วัส" + "ดร", each a real word on its own),
    which would hide exactly the typos this function exists to catch.
    "longest" only backs off to smaller pieces when nothing bigger matches
    at all, so an OOV misspelling like that survives as one token."""
    seen = set()
    typos = []
    for word in word_tokenize(markdown, engine="longest"):
        word = word.strip()
        if len(word) < _MIN_WORD_LEN or word in seen or word in _THAI_WORDS:
            continue
        if not _THAI_CHAR_RE.search(word):
            continue  # punctuation, numbers, Latin text, image alt text, ...
        seen.add(word)
        typos.append({"word": word, "suggestions": spell(word)})
    return typos
