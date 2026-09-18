# AjanDB webapp

A simple front end + backend for uploading unstructured files, storing them
as searchable markdown, and compiling a book from whatever matches a topic.

- **Data Store** — upload .pdf, .docx, .txt, or .md. PDFs go through the
  digitisation pipeline in `../pdf_to_docx_pipeline`; everything else is read
  directly. Two PDF engines, picked per upload from the UI: **Classical**
  (free, local Tesseract OCR + handwriting detection - the default) or
  **Vision-LLM** (a per-page call to Claude or GPT-4o - better on messy
  real-world scans and non-Latin scripts, costs tokens, needs an API key).
  Every upload is chunked to roughly one page per row and stored in SQLite
  tagged with the original filename, an upload timestamp, and a content
  category (typed in, or auto-suggested by an LLM call if left blank - falls
  back to "Uncategorized" with no API key rather than failing the upload).
  Each chunk carries an OCR/transcription confidence score (0-100, or n/a for
  native text that was never guessed) and a `needs_review` flag - set
  whenever a page contains handwriting or a low-confidence OCR region the
  pipeline couldn't transcribe reliably, or its average confidence drops
  below 75 - so uncertain conversions surface for a human to check instead
  of silently passing as fact. Conversion runs in a background thread and
  `POST /api/upload` returns a job id immediately - the frontend polls
  `GET /api/upload/{job_id}` every couple of seconds until it's done, so a
  many-minute OCR job never depends on one HTTP connection staying alive
  the whole time (real books can take that long; tunnels and browsers tend
  to give up on a single long-lived request well before that). Progress
  (page 1 converting, page 2 converting, ... saved) prints to the server's
  own console/log as it happens, **and** shows live in a small terminal-style
  console under the upload button in the page itself - no need to check the
  server's log just to see it isn't hung.

  **Review each page before saving** - a checkbox next to the upload
  button, PDF only. Instead of converting the whole file up front and
  flagging uncertain pages afterwards, this converts and shows one page at
  a time: the scanned page image on the left, the converted markdown in an
  editable textarea on the right, with its confidence score and review
  flag. Every exact bit of text that dragged that page's confidence below
  100% - a shaky OCR line or a self-doubting vision-LLM transcription - is
  highlighted right there in the text box, so you know exactly what to
  check first instead of re-reading the whole page; fix a highlighted line
  and its highlight disappears the moment your edit no longer matches the
  original flagged text. A **🔊 Read aloud** button reads the page's text
  out loud one paragraph at a time (the browser's own text-to-speech - no
  API key, no cost) and highlights whichever paragraph is currently being
  spoken in a second colour, so you can listen while following along
  against the scan instead of only reading silently.

  **🔍 Verify with second model** cross-checks the page against a vision-LLM
  call from whichever provider you pick (defaulting to whichever one *wasn't*
  used for the original conversion), and shows a word-level diff plus an
  agreement percentage - a much more trustworthy signal than either engine's
  own confidence score alone, since a vision-LLM's confidence is just the
  model guessing how sure it is (see "What does the confidence % actually
  mean?" below). It's opt-in, not automatic, since it costs a real API call
  - a hint appears on pages that are already flagged, exactly where a second
  opinion is worth that cost. Doesn't touch approve/skip/save state; safe to
  run more than once, and the diff panel is purely informational (it doesn't
  overwrite your editable text - copy over anything you agree with by hand).

  Thai text is also checked against [pythainlp](https://pythainlp.org)'s
  dictionary: any word it doesn't recognise gets a red wavy underline in the
  text box, and a suggested correction appears in a hint line below (e.g.
  `Possible Thai typo(s): "สวัสดร" → "สวัสดี"`). Same "fix it and the
  highlight disappears" behaviour as the confidence flags. It's a dictionary
  lookup, not real language understanding, so a genuine word it just doesn't
  know - a proper noun, slang, a loanword - gets flagged like a typo too;
  treat it as a hint to double-check, not a verdict.

  Images, handwriting,
  and low-confidence OCR regions are saved as real `.jpg` files and linked
  from the page's markdown rather than transcribed - see "Where is the
  output saved?" below for exactly how. Nothing is written to the database
  until you click **Approve & Save** - fix a transcription mistake before
  it's stored, or **Skip** a page you don't want kept at all. Each
  approved page is saved immediately (one `INSERT` per page, not a batch
  at the end), so closing the tab or hitting **Cancel review** partway
  through a long book keeps everything approved so far - already in the
  database and searchable - and only costs the pages not yet reviewed. The
  same in-page console tracks this flow's progress too (converting page N,
  ready for review, saved/skipped). If converting a page fails (e.g. a
  transient vision-LLM API error), a **Retry this page** button appears -
  it re-attempts only that page, never re-saving or silently skipping one
  that already succeeded. The same highlighting (and embedded images,
  rendered as real pictures since this view is read-only) also appears on
  a freshly-completed bulk upload's chunk previews below the upload button
  - it isn't retroactively computed for chunks browsed later from the
  database, since that data isn't stored.
- **Data Search** — full-text keyword search (SQLite FTS5) over every stored
  chunk, returning matches with their filename, page number, category,
  confidence, review flag, and a highlighted snippet. Below the search box,
  **Browse all books** lists every uploaded source file as one entry each
  (scanned/OCR'd PDFs and plain text/markdown alike) with its page count,
  category, average confidence, and how many pages are flagged for review.
  **View pages** expands it in place to show every page's converted text
  (same cards as a fresh upload); **Export .md** downloads that book's
  full text as a single markdown file, pages in order under `## Page N`
  headings - the only way converted text leaves the database as an actual
  file (see "Where is the output saved?" below).
- **Data Generation** — give it an instruction (e.g. "compile a book from all
  the files containing content about how to raise a child"); it keyword-
  searches the store for matching chunks and asks Claude or GPT (your choice,
  per request) to synthesise them into one coherent book in markdown.

## Deploy / redeploy (one command)

```bash
bash webapp/deploy.sh          # local target (default) - the fully-supported path
bash webapp/deploy.sh vercel   # vercel target - read the limitations below first
```

**`local` (default)** - installs any missing system dependencies (git,
Tesseract + language packs, ffmpeg), creates the venv on first run,
installs/updates Python packages, pulls the latest code (skipped
automatically if you have uncommitted local changes - it never discards
work), restarts the server, and opens a public `cloudflared` tunnel. Safe
to re-run any time; every step is idempotent. Ctrl+C stops both the tunnel
and the server. Everything (uploads, both OCR engines, search, compile)
works exactly as developed on this path.

Use `bash webapp/deploy.sh --no-pull` (either target) to redeploy what's
already on disk without touching git (useful right after editing something
locally).

**`vercel`** - runs `vercel deploy` (installs the `vercel` CLI yourself
first: `npm install -g vercel`) after printing a confirmation prompt, since
this app doesn't actually work correctly there yet:

- **SQLite storage doesn't persist.** Vercel serverless functions have an
  ephemeral, per-invocation filesystem - an upload written by one request
  is gone by the next. There's no real library on Vercel with this
  codebase as-is.
- **The classical OCR engine can't run.** It shells out to the `tesseract`
  binary, which isn't in Vercel's Python runtime and can't be apt-installed
  there. Only the vision-LLM engine (Claude/GPT-4o via API key) could work.
- **Background upload jobs don't survive.** `/api/upload` hands OCR to a
  background thread and returns immediately (see "Data Store" above); a
  serverless function has nothing running once it returns a response, and
  real OCR would usually exceed Vercel's function timeout anyway.

In short: `vercel` is good for a quick look at the UI, not for real
uploads/search/library use - use `local` for that. Turning this into a
real serverless deployment would mean swapping SQLite for a hosted
database and reworking the upload-job/OCR-engine story around Vercel's
constraints; that's a bigger change than this script and hasn't been done.
Pass `--yes` to skip the confirmation prompt (scripted use) and `--prod`
to pass `--prod` through to `vercel deploy`. If `webapp/vercel.json`
doesn't exist yet, a minimal one is created for you.

Drop a `webapp/.env` (copy `webapp/.env.example`) with your API key(s) if you
don't want to use the in-page "API Keys" panel or re-export them every time:
```bash
cp webapp/.env.example webapp/.env
# then edit webapp/.env
```

<details>
<summary>Manual setup (what deploy.sh does, if you want to run it by hand)</summary>

```bash
cd webapp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn backend.main:app --reload --app-dir .
```

PDF uploads need the same system dependencies as `pdf_to_docx_pipeline`
(Tesseract + language packs, Noto fonts) - see that directory's README.
</details>

Then open http://127.0.0.1:8000/ (or the `cloudflared` URL `deploy.sh`
prints) - the backend serves the frontend directly, so there's nothing
separate to run for the UI.

The "Data Generation" tab, and the Vision-LLM upload engine, need an API key
- pick whichever provider you actually have (neither is the same thing as a
claude.ai or ChatGPT Plus *subscription*; both are separate, billed-by-usage
API keys from that provider's own developer console). Two ways to supply
one:

- **Type it into the page** - the "API Keys" panel at the top stores it in
  that browser's `localStorage` only (never sent anywhere but this page's
  own backend, never persisted server-side or in the database, never logged).
  Convenient when you're the only user and don't want to fuss with shell
  environments.
- **Set it in the backend's environment** - the usual way, shared by every
  visitor of this instance:
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...   # for the Claude option
  # and/or
  export OPENAI_API_KEY=sk-...          # for the GPT option
  ```

A browser-supplied key takes priority over the environment for that request;
falls back to the environment when the field is left blank. The frontend
lets you pick Claude or GPT per upload/compile request; without a usable key
either way, that request returns a clear error rather than crashing (tested;
the actual model calls are mocked in the test suite since no key is
available in this environment). `$LLM_PROVIDER` / `$VISION_LLM_PROVIDER` set
the server-side default provider when the frontend doesn't specify one.

## Test

```bash
python3 -m pytest tests/ -v
```

169 tests total (42 in the pipeline, 127 here) covering the SQLite/FTS5 layer
(including confidence/needs_review/category columns, and the book-browsing
queries' handling of duplicate/re-uploaded pages), chunking (per-page for
PDFs, character-budget for plain text, which exact snippets get flagged for
the review UI's highlighting, and image files being written/reused/served
correctly), category suggestion (mocked model calls, graceful no-key
fallback), the book compiler's retrieval + error handling for both
providers (plus both stripping embedded image links before sending
anything to a model), browser-supplied key resolution/priority, the
background upload-job lifecycle, the page-by-page review session's state
machine (approve/skip/retry/cancel, including recovering from a failed
page conversion without duplicating or losing work, and the second-opinion
word-diff verification), Thai spell-checking (correctly-spelled text,
flagged typos with suggestions, non-Thai tokens ignored, duplicates
collapsed), the FastAPI endpoints via `TestClient`, and the standalone
chunked-upload script below.

## Digitising a whole book without the web upload (`scripts/chunked_upload.py`)

For a large scanned book, there's a second way in besides the browser's
Data Store tab:

```bash
cd webapp
python3 scripts/chunked_upload.py /path/to/book.pdf
```

This bypasses the web server and `/api/upload` entirely. It opens the PDF
directly, splits it into page-range batches (5 pages by default), converts
each batch with the same `backend/convert.py` pipeline the web app uses, and
inserts the resulting chunks straight into `data/ajandb.sqlite3` as soon as
each batch finishes - so pages already processed show up in search
immediately, even while later pages are still converting. No HTTP upload of
the source file, and no tunnel connection needs to survive the OCR run at
all, since none of the work happens over HTTP.

If it's interrupted partway through a long book (closed terminal, crashed
process, killed to free up the machine), re-run with `--resume` and it skips
every page range it already finished, picking up where it left off instead
of reprocessing the whole book:

```bash
python3 scripts/chunked_upload.py /path/to/book.pdf --resume
```

Useful options (see `--help` for the full list):

- `--chunk-size N` - pages per batch (default 5; smaller batches mean more
  frequent progress/resume points, larger batches mean fewer temp files).
- `--engine vision --provider anthropic|openai --api-key ...` - use the
  vision-LLM engine instead of local Tesseract, same tradeoffs as the web
  upload's engine picker (needs an API key; better on messy scans, costs
  tokens). Without `--api-key`, falls back to `ANTHROPIC_API_KEY`/
  `OPENAI_API_KEY` in the environment.
- `--category "My Book"` - skip auto-suggestion and use this category for
  every chunk. Left unset, it auto-suggests once (same as the web upload)
  and reuses that category on every later batch - including a `--resume`
  run, which looks up whatever category the file's earlier chunks already
  got instead of re-suggesting from a different page range.
- `--stop-on-error` - abort on the first batch that fails to convert,
  instead of the default (log it, keep going, report all failures at the
  end so one bad page range doesn't lose the rest of the book).

Progress state lives in `data/chunked_upload_state/` (gitignored), one JSON
file per input PDF. Without `--resume`, a re-run reprocesses and re-inserts
every page from scratch - it doesn't delete anything first, so running it
twice on the same book without `--resume` leaves duplicate chunks; use
`--resume`, or clear out that book's rows via `/api/documents` first, if
that's not what you want.

## Data storage

Everything lands in `data/ajandb.sqlite3` (gitignored - it's your local
library, not something to commit) plus extracted images as real files
under `data/images/` (see below) - delete both to start fresh. Deleting
just the database without the images (or vice versa) leaves orphaned
files/broken links; there's no cleanup tool for that yet.

**What does the confidence % actually mean?** Different things depending
on the engine, worth knowing before you trust it:

- **Classical (Tesseract)**: a real, per-character recognition score from
  the OCR engine itself, averaged per line then per page. A genuine
  statistical signal - reasonably well-calibrated as a *relative* ranking
  (90% really is more trustworthy than 60%), though still just one
  engine's own self-measurement, not independently verified accuracy.
- **Vision-LLM (Claude/GPT-4o)**: the model is literally asked to guess a
  number for "how sure am I" (see the prompt in `pipeline/vision_ocr.py`).
  This is **not calibrated** - LLM self-reported confidence is well known
  to correlate poorly with actual accuracy, and models tend toward
  overconfidence. Treat it as a rough hint, never as ground truth.

`needs_review` fires when average confidence drops below 75%, or
unconditionally whenever handwriting or a low-confidence region was
found, regardless of the number. **Verify with second model** (see "Review
each page before saving" above) exists specifically to give you a better
signal than either engine's own confidence alone: independent agreement
between two separately-run models is much stronger evidence than one
model's self-report, and the diff shows exactly which words to check if
they don't agree. It isn't proof either, though - both models can share
the same blind spot (e.g. genuinely illegible handwriting) and
confidently agree on the same wrong answer.

**Where is the output .md saved?** Nowhere, by default - converted
markdown is stored purely as text in that SQLite file, one row per chunk,
never written out to a `.md` file on disk. Data Search's **Export .md**
(see above) is the way to get one: it concatenates a book's chunks in page
order into a single downloadable markdown file. `pdf_to_docx_pipeline`'s
own CLI (`python -m pipeline.main input.pdf output.docx`) is a separate
tool that writes a `.docx`, not `.md`, and isn't part of this webapp.

**What happens to images, handwriting, and low-confidence regions?**
Each is decoded and re-saved as a real `.jpg` file under `data/images/`
(named by a hash of its original bytes, so the exact same image - e.g.
from re-uploading the same book - reuses the existing file instead of
writing a duplicate), and referenced from the markdown as
`![alt](/images/<hash>.jpg)` in its original reading-order position - a
real picture, not a "see the original document" placeholder note. The
`/images/<hash>.jpg` path is served directly by the backend, so it
resolves correctly wherever you're browsing the app from (localhost, a
`cloudflared` tunnel, ...) and renders as an actual `<img>` everywhere:
the page-by-page review textarea (as a short, one-line link - a
`<textarea>` still can't show an inline image, but there's no longer a
wall of base64 text to deal with either), a freshly-completed bulk
upload's preview, and Data Search's "View pages". **Export .md** rewrites
each link to a full URL (against whatever host served that request)
before download, since a saved file has no "current page" to resolve a
host-relative link against - opening the export elsewhere, or after the
server has stopped, will show broken image links; there's no way around
that short of bundling the image files with the export too, which isn't
done. Handwriting and low-confidence regions are also flagged for the
review UI's highlighting (see "Review each page before saving" above), the
same as an uncertain OCR text line; a plain photo isn't.

**Re-uploading the same file:** nothing here deduplicates chunks at
insert time - re-uploading a book (or `chunked_upload.py` re-running a
batch without `--resume`) adds a second row per page rather than
replacing the first (see that script's own README section). Browsing and
exporting only ever show the *latest* row per page, though, so a stray
duplicate from a re-run doesn't show up twice or skew a book's average
confidence/review count - it's just an unused extra row taking up (very
little) space until you clean it out by hand if it ever bothers you.

**Schema note:** this replaced an earlier one-row-per-file schema
(`filename`/`markdown` only, no chunking/confidence/category). There's no
migration - if you have an old database file from before this change,
delete `data/ajandb.sqlite3` and re-upload; nothing here reads the old
column layout.

## Known limitations

- Search/retrieval for the book compiler is keyword-based (SQLite FTS5), not
  semantic - it finds files containing your topic's words, not files that
  are conceptually related without using them. Fine for a personal library
  at moderate scale; a vector index would be the natural upgrade if keyword
  search starts missing relevant files.
- Images, handwriting, and low-confidence OCR regions become real `.jpg`
  files under `data/images/`, linked from the markdown - not transcribed,
  and never cleaned up automatically. Deleting a chunk (or the whole
  database) doesn't delete the image files it referenced; re-uploading a
  file you've already uploaded reuses existing images by content hash
  rather than duplicating them, but there's no garbage collection for
  images that are no longer referenced by anything. Fine for a personal
  library; worth knowing if `data/images/` ever needs manual tidying.
- The book compiler and category auto-suggestion both strip embedded image
  links down to a short `[label]` placeholder before sending anything to
  the model, since a markdown image link isn't meaningful prose either
  way - but the book compiler still sends up to 20 matching chunks (8000
  chars each, post-stripping) per request, so a very large personal
  library on a broad topic could still exceed that and only see a partial
  set of matches.
- **Read aloud** (in page-by-page review) uses the browser's own
  `speechSynthesis` API - no server call, no API key, but voice
  availability/quality (especially for Thai and other non-English text)
  depends entirely on what the browser/OS provides. It reads one paragraph
  at a time and highlights the one currently being spoken; embedded image
  links are skipped rather than read aloud as a URL.
- Audio and video upload aren't supported yet (speech-to-text would need
  OpenAI's Whisper API specifically - Claude has no audio API - plus
  `ffmpeg` for video). Deliberately deferred rather than half-built.
- The vision-LLM engine's confidence score is the model's own self-reported
  estimate, not a calibrated metric like Tesseract's - treat it as a rough
  signal, not ground truth.
- Upload job status is kept in memory, not the database - a server restart
  mid-conversion loses that job (re-upload the file). Fine for a
  single-process personal tool; a persisted job table would be the fix if
  this ever runs somewhere restarts are frequent. The page-by-page review
  session has the same tradeoff: a restart mid-review loses the session
  (start the review again) - but every page already approved before that
  restart was already saved to the database, so nothing reviewed is lost.
- `/api/chat` (Data Generation) is still a single synchronous request - for
  a very large matched-source set this could hit the same
  long-connection/tunnel-timeout problem uploads used to have. Not yet
  observed in practice (compiling is normally one fast model call), but the
  same background-job pattern used for uploads would be the fix if it comes
  up.
