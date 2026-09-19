# AjanDB — Project Context & Architecture

2026-09-19 · @apinyacode

## Overview

AjanDB is a personal knowledge-base webapp for digitising and searching scanned/born-digital
documents (field notes, forms, books), with a focus on bilingual English/Thai content. The
repo has two parts:

- **`pdf_to_docx_pipeline/`** — the PDF digitisation engine. Classical scripting and small
  local models wherever possible; an "AI" (vision-LLM) step is treated as an expensive
  resource spent only where nothing else works.
- **`webapp/`** — a FastAPI + vanilla JS/HTML/CSS app (no build step, SQLite+FTS5 storage)
  with three tabs: **Data Store** (upload/convert/review), **Data Search** (full-text search
  + browse/export), **Data Generation** (LLM-synthesised books from stored content).

## Architecture

**Pipeline** (`pdf_to_docx_pipeline/pipeline/`) — four stages, each only as expensive as the
content actually requires:

```
PDF
 ├─ Stage 1 (extract.py)     Real text layer? YES → read text+images directly (zero OCR)
 │                                             NO  → rasterize page, hand to Stage 2
 ├─ Stage 2 (ocr.py)          Classical OpenCV cleanup (deskew/denoise/binarize)
 │                            → local Tesseract 5 OCR, multi-language
 ├─ Stage 3 (handwriting.py)  Classical stroke-geometry check (stroke-width variance +
 │                            baseline-wander, no ML): does this look handwritten?
 └─ Stage 4 (assemble.py)     Rebuild in reading order: text → paragraph; picture →
                              embedded image; handwriting → embedded image + review flag
```

There is no reliable non-ML technique for reading handwriting, so the deliberate default is
**don't guess — preserve it as an image, flagged for a human to transcribe**, with OCR-based
transcription as an optional, explicitly separate second pass (`main_vision.py`, below).

`vision_ocr.py` adds an alternative to Stages 2–3: a per-page vision-LLM call (Claude or
GPT-4o) instead of local Tesseract — better on messy real-world scans and non-Latin scripts,
at the cost of tokens and an API key. `main.py` (classical) and `main_vision.py` (vision)
both orchestrate `doc_items` output with a `progress` callback.

**Zero-AI vs. small-local-model vs. LLM, precisely:** born-digital extraction, all OpenCV
preprocessing, the handwriting heuristic, Thai spacing cleanup, and `.docx` assembly touch
no model at all. Tesseract 5's recognition step is technically a small local LSTM (not pure
pattern-matching) but runs on CPU in well under a second per page with no network call and
no per-token billing — the deliberate trade this pipeline makes throughout: a small local
model instead of a large remote one, only for the pages that actually need it. A vision-LLM
call is the explicit last resort. **Recommended policy:** try `main.py` (classical) first —
free, and often good enough for clean typed documents; fall back to `main_vision.py` only
for documents/pages where the classical engine's `low_confidence_regions` /
`handwritten_lines` counts come back high, rather than defaulting to vision for everything.
The biggest environmental lever isn't which OCR engine you pick, it's how many pages need
OCR at all — Stage 1's CLI output makes that visible (pages needing OCR vs. not) rather than
running everything through OCR "to be safe."

**Conversion** (`webapp/backend/convert.py`) turns `doc_items` into one **chunk per page** —
`{page_number, markdown, confidence, needs_review, flagged_snippets}`. Native PDF text is
always `confidence=100`; anything OCR'd/guessed is `<100` and its exact text is added to
`flagged_snippets` (substrings the review UI highlights). Images/handwriting/uncertain
regions are never transcribed — each is saved as a real `.jpg` under
`webapp/data/images/<sha256-hash>.jpg` (deduped by content hash) and referenced from the
markdown as `![alt](/images/<hash>.jpg)`, served by a plain `GET /images/{filename}` route
(not a `StaticFiles` mount, so tests can monkeypatch the images directory).

**Review session state machine** (`webapp/backend/review.py`) — in-memory `_sessions` dict
keyed by session id. `start()` converts page 0; `approve()`/`skip()` resolve the pending page
then convert the next (`_advance()`); `retry()` re-attempts a failed conversion; `cancel()`
ends the session, keeping already-approved pages. Every response includes a running `log`
for the frontend's live console.

**Storage** (`webapp/backend/db.py`) — SQLite + FTS5. `list_books()` / `get_book_chunks()`
group chunks by `(source_filename, total_pages)` and dedupe re-uploaded pages via a
`_LATEST_PER_PAGE_CTE` (most-recent row per page wins) — re-uploading doesn't delete the
old row, it just becomes an unused extra one that browsing/export ignore.

**Text-to-speech** (`webapp/backend/tts.py`) — `get_or_synthesize(text)` fetches an Azure
Speech access token, POSTs SSML to Azure's TTS REST endpoint, and caches the returned MP3
under `webapp/data/audio/<sha256-hash-of-text>.mp3`, mirroring `convert.py`'s image-caching
pattern exactly (same hash-by-content-then-check-if-file-exists shape). Voice is chosen by
scanning the text for Thai-script characters rather than trusting a caller-supplied language
hint. Served by a plain `GET /audio/{filename}` route (same reasoning as `/images/{filename}`
— not a `StaticFiles` mount, so tests can monkeypatch the audio directory).

**Frontend** (`webapp/frontend/app.js` + `index.html` + `style.css`) — no framework, no
build step. Central technique is a **highlight-overlay**: an `aria-hidden` div sits behind
the editable review `<textarea>`, sharing identical font/padding/box-sizing, rendering the
same text with `<mark class="...">` around flagged spans (transparent text, colour via
background/underline only) while the textarea's real text shows through on top — editing a
flagged span makes its highlight disappear once the edit no longer matches.

## Key features

- **Data Store** — upload `.pdf`/`.docx`/`.txt`/`.md`. PDFs go through the pipeline above
  with a choice of engine (Classical/free vs. Vision-LLM/paid). Conversion runs as a
  background job (`POST /api/upload` returns immediately, frontend polls) so a many-minute
  OCR run never depends on one HTTP connection staying alive.
- **Page-by-page review** — converts and shows one PDF page at a time (scan image left,
  editable markdown right) before saving; nothing hits the database until **Approve & Save**,
  and each approved page is saved immediately (not batched), so cancelling partway through
  a long book keeps everything approved so far.
- **Multi-signal "likely wrong" flagging** (review flow only, not bulk upload — see below;
  opt-in via the Data Store tab's **Second-model validation** checkbox, off by default — see
  below) — when turned on, `flagged_snippets` becomes a *union* of up to four independent
  signals for any page below 100% confidence, not just the original per-line confidence flag,
  since no single signal is fully trustworthy alone (see Known limitations):
  1. **Per-line confidence** (original, always on regardless of the toggle) — any text whose
     own OCR/vision confidence is <100%.
  2. **Automatic cross-provider check** (`review._cross_provider_disagreement_flags`) — a
     second vision-LLM call against whichever provider *wasn't* used for the primary
     conversion, diffed the same way "Verify with second model" always has been. Always
     resolves its key from the backend's environment (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) —
     there's no browser-supplied-key path for a provider the session isn't already using — so
     it's silently skipped without one, never blocking a page.
  3. **Tesseract cross-check** (`review._tesseract_cross_check_flags`, vision-engine pages
     only) — runs classical OCR on the same rendered page image already used for the
     vision-LLM pass and flags disagreements. Free and local, no API key needed, so it always
     runs (unlike #2) whenever the engine is vision.
  4. **GPT-4o logprob check** (`review._openai_logprob_flags`, only when the page's own
     provider is `openai`) — re-sends the page requesting per-token log-probabilities
     (`extract_page_with_vision_logprobs` in `vision_ocr.py`) and flags any transcribed block
     whose average logprob falls below a fixed threshold (`_LOGPROB_FLAG_THRESHOLD = -1.5`).
     Anthropic's API doesn't expose per-token logprobs, so there's no equivalent for that
     provider.

  All four are unioned and exact-duplicates removed (`review._dedup_flags`) before reaching
  the frontend; a disagreement from #2-4 alone (even with a high self-reported confidence
  score) is itself enough to set `needs_review`. Highlighted via the same overlay technique
  as before; fixing a flagged span makes its highlight disappear.

  **Bounded, concurrent cross-checks** (`review._run_cross_checks_with_timeout`) — #2 and #4
  are each an extra API call stacked on top of the page's own primary conversion, on exactly
  the pages where that primary call is already slowest (vision-engine pages); run
  sequentially, this was long enough on a real deployment to push a single review-page
  request past a tunnel/proxy's own timeout, which surfaced to the reviewer as a broken
  `<!DOCTYPE ...> is not valid JSON` error instead of a slow-but-working page (a real bug hit
  in beta testing, not a hypothetical). Fixed by running every API-calling cross-check
  concurrently in its own thread, capped at one shared `_CROSS_CHECK_TIMEOUT_SECONDS` (20s)
  budget total rather than per-check - a cross-check that doesn't finish in time is simply
  abandoned (skipped, not an error; its thread keeps running in the background and its result
  is discarded once it does finish). The page's own primary conversion is never subject to
  this budget - only the optional cross-checks are.

  **Opt-in toggle** — `review.start(..., auto_validate=False)` (default off; an earlier
  version of this feature ran automatically whenever an env key was configured, with no way
  to turn it off short of removing the key - that's what caused the tunnel-timeout bug
  above). The Data Store tab's **Second-model validation** checkbox (`#auto-validate-checkbox`
  in `index.html`, sent as the `auto_validate` form field to `POST /api/review/start`) is the
  per-upload opt-in; it's disabled/unchecked until **Review each page before saving** itself
  is checked, since it only means anything inside a review session.
- **Real `.jpg` image storage** — images/handwriting/uncertain regions are saved as actual
  files (not base64) in reading order, deduped by content hash. (This replaced an earlier
  base64-embedded approach after explicit feedback that files-on-disk were preferable.)
- **Read Aloud (TTS)** — server-side via Azure Speech (`webapp/backend/tts.py`), paragraph
  by paragraph (word-level highlighting was skipped as unreliable across browsers, especially
  for Thai); the currently-spoken paragraph is highlighted in a second colour via the same
  overlay. Embedded image lines are skipped rather than read aloud as a URL. Voice (Thai vs.
  English neural voice) is picked automatically from the text itself (presence of Thai-script
  characters), not a language hint from the frontend, since a page routinely mixes both per
  paragraph. Generated audio is cached under `data/audio/<sha256-hash-of-text>.mp3` — the
  same dedup pattern `convert.py` uses for images — so replaying a paragraph never re-hits
  the billed API. Originally browser-native `SpeechSynthesisUtterance`; replaced because
  native voice availability/quality varies wildly by OS/browser, especially for Thai.
  Toggleable per upload via the Data Store tab's **Enable Read aloud** checkbox
  (`#enable-read-aloud-checkbox`, checked by default, disabled until review mode is on) -
  a frontend-only gate (it just shows/hides the "🔊 Read aloud" button in `showReviewPage()`),
  since the feature is already fully opt-in by nature - a reviewer has to click the button
  for it to do anything - so this toggle is really just "hide the button entirely," e.g. for
  a deployment with no Azure key configured at all.
- **Verify with second model** — opt-in cross-check of a flagged page against a *different*
  vision-LLM provider than the one used originally; shows a word-level diff
  (`difflib.SequenceMatcher` on whitespace-preserving tokens) and an agreement percentage.
  Doesn't touch approve/skip state; safe to re-run. This exists specifically because a
  vision-LLM's own confidence score is not calibrated (see Known limitations).
- **Thai spell-check** — `webapp/backend/spellcheck.py` flags Thai words missing from
  `pythainlp`'s dictionary with a red wavy underline + a suggested-correction hint. Uses the
  `"longest"` tokenizer engine specifically because the default `newmm` engine silently
  shatters a genuinely misspelled word into smaller *valid* syllables before a dictionary
  check ever sees it (e.g. `สวัสดร` → `ส` + `วัส` + `ดร`, each individually a real word) —
  `"longest"` only backs off to smaller pieces when nothing bigger matches, so a misspelling
  survives as one token. Advisory only — a real word the dictionary doesn't know (proper
  noun, slang, loanword) gets flagged too.
- **Book browsing/export** — groups uploaded pages into "books," dedupes re-uploaded pages,
  `.md` export rewrites relative image links to absolute URLs (a downloaded file has no
  "current page" to resolve a relative link against).
- **Data Generation** — keyword-searches stored chunks and asks Claude/GPT to synthesise a
  coherent book from the matches; strips embedded image markdown down to a `[label]`
  placeholder before sending anything to an LLM.
- **`scripts/chunked_upload.py`** — ingests a large book outside the browser/HTTP entirely:
  splits into page-range batches, converts and inserts each batch as it finishes (pages show
  up in search immediately), and supports `--resume` to pick up after an interruption instead
  of reprocessing the whole book.

UI note: the review editor/highlight-overlay font size was bumped 10% (0.85rem → 0.935rem)
for readability, alongside the spell-check feature.

## Conventions and patterns

**Test isolation.** `webapp/tests/conftest.py` has a global autouse fixture
`_isolate_images_dir` (monkeypatches `convert.IMAGES_DIR` to a tmp path) after real `.jpg`
files were once written into the actual `data/images/` during test runs. `test_review.py`
similarly isolates the DB connection and clears the in-memory `review._sessions` dict before
and after every test.

**Live verification workflow**, repeated after every feature: kill any stale `uvicorn`,
delete `webapp/data/ajandb.sqlite3` and `data/images/` for a clean slate, start the server in
the background, confirm `GET /api/documents` returns `[]`, build the synthetic 3-page test
PDF via `pdf_to_docx_pipeline/make_test_pdf.py`'s `build()` (page 1: born-digital bilingual
text; page 2: scanned/OCR-needed bilingual text; page 3: born-digital heading + embedded
photo + simulated handwriting), then drive real headless Chromium via Playwright to click
through the UI, assert on DOM/network state, and screenshot for visual confirmation. Scratch
scripts/PDFs/screenshots and real `data/` artifacts are cleaned up afterward.

**Commit convention:**
```
<what changed, imperative mood>

<why, if not obvious from the diff itself>

Co-Authored-By: Claude <noreply@anthropic.com>
Claude-Session: <link>
```

**Design philosophy** consistently applied through the build: minimal abstractions (no
premature helpers), no speculative error handling, comments only for non-obvious *why*
(never restating *what*), and features validated live in a browser rather than assumed
correct from passing tests alone.

## Current state

**196 tests pass** (44 in `pdf_to_docx_pipeline`, 152 in `webapp`), covering: the SQLite/FTS5
layer and duplicate-page dedup, per-page/per-character-budget chunking and image handling,
category suggestion with graceful no-key fallback, the book compiler's retrieval and error
handling for both providers, the review session state machine (including recovery from a
failed page conversion and the second-opinion word-diff), Thai spell-checking, server-side
TTS (voice selection, caching, the token-fetch + synthesize call chain, the `/api/tts` and
`/audio/{filename}` endpoints — all against a mocked Azure client, no billed calls in CI),
the multi-signal flagging union (each of the four signals firing/not-firing independently,
gated correctly by engine/provider, and deduped when they overlap), and the FastAPI endpoints
via `TestClient`.

**Shipped, in build order:** upload/search/chat webapp → per-page chunking with OCR
confidence → async uploads → page-by-page review mode → live progress console → book
browsing/export → confidence highlighting → image embedding (base64, then corrected to real
`.jpg` files) → read-aloud with sync highlighting → verify-with-second-model → Thai
spell-check + font-size bump → **beta launch Phase 1** (`webapp/Dockerfile`) → **beta launch
Phase 2** (server-side TTS via Azure Speech) → **beta launch Phase 3** (multi-signal
flagging, see below).

**Known limitations:**

- **Vision-LLM confidence is the model's own self-reported estimate — explicitly *not*
  calibrated**, unlike Tesseract's real per-character score (well-calibrated as a *relative*
  ranking, not independently verified accuracy either). This is why flagging no longer relies
  on it alone: any page below 100% confidence can also go through up to three independent
  cross-checks (cross-provider, Tesseract, GPT-4o logprobs - see "Multi-signal 'likely
  wrong' flagging" above, opt-in via the **Second-model validation** checkbox), and a
  disagreement from any of them sets `needs_review` even when the page's own confidence score
  was high. Residual limitations of *that* approach:
  - All the cross-checks can still share the same blind spot (e.g. genuinely illegible
    handwriting) and confidently agree on the same wrong answer - agreement between
    signals is much stronger evidence than one score alone, but still isn't proof.
  - The cross-provider check and the GPT-4o logprob check each cost a real, extra API call
    per flagged page (on top of the page's own original conversion call) - unlike the free,
    local Tesseract cross-check. This feature briefly ran automatically-by-default rather
    than opt-in (see the beta launch task list), which is exactly what caused the
    tunnel-timeout bug fixed above - it's opt-in again now specifically to put that
    cost/latency tradeoff back in the reviewer's hands rather than triggering it silently
    whenever a key happens to be configured. The bounded-concurrency fix above still applies
    whenever it *is* turned on.
  - Multi-signal flagging only runs in the interactive page-by-page review flow
    (`review.py`), not the background bulk-upload path (`POST /api/upload` /
    `convert.convert_to_markdown`) - deliberately: bulk upload has no human pacing it
    page-by-page, and enabling an automatic extra-API-call-per-page multiplier unattended
    across a whole book felt like a cost surprise worth a separate, explicit decision
    rather than folding in silently. Bulk-uploaded pages still get the original per-line
    confidence flag, just not the three cross-checks.
- Thai spell-check is a dictionary lookup, not language understanding — proper nouns, slang,
  and loanwords absent from the dictionary get flagged like typos too. Advisory only, never
  blocks approval.
- Search/retrieval for the book compiler is keyword-based (SQLite FTS5), not semantic. Fine
  at personal-library scale; a vector index would be the natural upgrade if keyword search
  starts missing relevant files. The compiler also sends at most 20 matching chunks (8000
  chars each, post-image-stripping) per request, so a very large library on a broad topic
  could see only a partial match set.
- Images/handwriting/uncertain regions become real `.jpg` files under `data/images/` and are
  never garbage-collected — deleting a chunk or the whole database doesn't delete the image
  files it referenced. Fine for a personal library; worth knowing if that directory ever
  needs manual tidying.
- Review sessions and background upload-job status live in memory only, not the database —
  a server restart mid-conversion loses that job/session (already-approved/saved pages are
  safe either way).
- Audio/video upload isn't supported (would need Whisper specifically, since Claude has no
  audio API, plus `ffmpeg` for video) — deliberately deferred rather than half-built.
- The Vercel deploy target doesn't actually work correctly yet: SQLite storage doesn't
  persist on Vercel's ephemeral per-invocation filesystem, the classical OCR engine can't run
  (no `tesseract` binary in that runtime), and background upload jobs don't survive a
  serverless function returning. Good for a quick UI look, not for real use — the `local`
  deploy target is the fully-supported path.
- `/api/chat` (Data Generation) is a single synchronous request — for a very large matched-
  source set this could in principle hit the same long-connection/timeout problem uploads
  used to have; not yet observed in practice, and the same background-job pattern used for
  uploads would be the fix if it comes up.
- **Pipeline: short-text language tagging can misfire.** `langdetect` is a statistical
  n-gram model and occasionally misclassifies short titles (a 6-word English heading was
  tagged as German in testing); fine on full sentences, shakier under ~10 words.
- **Pipeline: Thai OCR has real, expected error rates.** Tesseract's Thai model inserts a
  space between almost every character (Thai doesn't space between words); a regex cleanup
  (`normalize_thai_spacing`) handles that, but individual character misreads (e.g. a
  tone-mark vowel) still happen, same as any OCR engine on non-Latin scripts.
- **Pipeline: the handwriting heuristic is untrained, not a classifier.** Combines
  stroke-width variance and baseline wander; scripts that stack diacritics (Thai,
  Vietnamese, Arabic, Devanagari) show naturally more baseline spread than Latin print, so
  it requires *both* geometric irregularity *and* low OCR confidence before flagging a line,
  rather than trusting geometry alone. `--handwriting-threshold` is exposed on the CLI for
  tuning against your own documents.
- **Pipeline: reading order isn't always perfect** for unusual multi-column layouts —
  PyMuPDF's `sort=True` helps but very complex layouts may need manual bounding-box logic.
- **Pipeline: the vision engine hasn't been run end-to-end against a real API key** in the
  environment this was built in (only mocked-response tests) — structurally verified, not
  accuracy-verified; run it on a few pages first and check the output before trusting it on
  a full document.

- `docker build`/`docker run` for `webapp/Dockerfile` could not be run live in the sandbox
  this was built in — its container runtime's registry pulls are blocked by that
  environment's own egress policy (Docker Hub CDN denied), unrelated to the Dockerfile
  itself. Verified instead by reproducing the image's exact dependency set (a clean venv
  from both subprojects' `requirements.txt`, no dev-only packages) and file layout (only
  `pipeline/`, `webapp/backend/`, `webapp/frontend/` — matching the Dockerfile's `COPY`
  lines) on the host directly, then running the full upload → review (approve/skip) →
  search → book-browse round trip against it with Playwright. Re-run `docker build -f
  webapp/Dockerfile -t ajandb .` (from the repo root) somewhere with normal registry access
  before relying on it for a real deploy.
- Fixed in passing: `pdf_to_docx_pipeline/requirements.txt` was missing `pillow` (`from PIL
  import Image` in `handwriting.py`/`make_test_pdf.py` only worked because it happened to
  already be installed in the dev venvs, e.g. as a manual install or another package's
  transitive pull) — a fresh install from `requirements.txt` alone would have failed at
  import time. Added explicitly.
- **Read Aloud's actual audio quality wasn't verified against a real Azure key** in the
  sandbox this was built in (no live network path to Azure was exercised there either) —
  `tts.py`'s HTTP calls, caching, and the frontend's fetch→`Audio`→highlight chain were all
  verified end-to-end with the Azure call itself mocked (unit tests plus a live Playwright
  click-through serving a pre-cached placeholder file in place of a real synthesis call).
  Do an actual listen test on real Thai and English text with a live `AZURE_SPEECH_KEY`
  before considering this feature done for testers.
- Read Aloud makes one Azure API call per not-yet-cached paragraph — a full page of
  never-before-read text costs that many calls. Fine for a personal beta; worth knowing if
  usage or cost ever needs watching.
- No cache eviction for `data/audio/` (same tradeoff as `data/images/` above) — generated
  clips accumulate indefinitely.

**Beta launch task list — status** (see `AjanDB_beta_launch_tasks.md` if still around, or
ask for it again): three phases — (1) hosting readiness, (2) server-side TTS, (3)
multi-signal "likely wrong" flagging.

- **Phase 1** — task #1 (`webapp/Dockerfile`) done, above. Tasks #2-5 (persistent volume,
  stable URL/TLS, scheduled backup, live smoke test against a deployed host) are on hold
  pending a host choice (Render/Railway/VPS) — each depends on host-specific config this
  doc can't usefully guess at ahead of that decision.
- **Phase 2** (server-side TTS, Azure Speech) — tasks #6-10 done: `tts.py` + `/api/tts` +
  `/audio/{filename}`, caching by text hash, frontend swapped off
  `SpeechSynthesisUtterance`, tests mocking the Azure call. Real-key listen test still
  outstanding (see Known limitations above).
- **Phase 3** (multi-signal flagging) — tasks #11-15 done: automatic cross-provider check,
  Tesseract cross-check, GPT-4o logprob check, unioned + deduped into `flagged_snippets`,
  Known limitations rewritten above. Scoped to the review flow only (not bulk upload) - see
  the residual-limitations bullet above for why.

**Not yet done / natural next steps:** a real-Azure-key listen test for Phase 2 (build is
done, quality unverified); Phase 1 tasks #2-5 (needs a host decision). All three beta launch
phases are otherwise complete - see the repo's `AjanDB_beta_launch_tasks.md` (if kept) for
the original acceptance criteria.

## Repository and links

- GitHub: [apinyacode/ajandb](https://github.com/apinyacode/ajandb), branch `main`.
- Local checkout in Claude Code sessions: `/home/user/ajandb` (`webapp/` +
  `pdf_to_docx_pipeline/`).
- Setup, run, and test commands: see each subproject's own `README.md` — this doc is
  architecture/decisions, not a setup guide.
- This doc is the standing reference to hand a fresh Claude Code session so it doesn't have
  to re-derive the design decisions above from scratch. Update it in the same session a
  change happens, not later — if "Not yet done" isn't genuinely current, it's actively
  misleading.
