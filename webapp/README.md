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
  (which page is converting, when it's saved, any error) prints to the
  server's own console/log as it happens - watch the terminal running
  `deploy.sh` (or `/tmp/ajandb_uvicorn.log`) during a long conversion to
  see it's actually working rather than hung.

  **Review each page before saving** - a checkbox next to the upload
  button, PDF only. Instead of converting the whole file up front and
  flagging uncertain pages afterwards, this converts and shows one page at
  a time: the scanned page image on the left, the converted markdown in an
  editable textarea on the right, with its confidence score and review
  flag. Nothing is written to the database until you click **Approve &
  Save** - fix a transcription mistake before it's stored, or **Skip** a
  page you don't want kept at all. Each approved page is saved immediately
  (one `INSERT` per page, not a batch at the end), so closing the tab or
  hitting **Cancel review** partway through a long book keeps everything
  approved so far - already in the database and searchable - and only
  costs the pages not yet reviewed. If converting a page fails (e.g. a
  transient vision-LLM API error), a **Retry this page** button appears -
  it re-attempts only that page, never re-saving or silently skipping one
  that already succeeded.
- **Data Search** — full-text keyword search (SQLite FTS5) over every stored
  chunk, returning matches with their filename, page number, category,
  confidence, review flag, and a highlighted snippet.
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

125 tests total (42 in the pipeline, 83 here) covering the SQLite/FTS5 layer
(including confidence/needs_review/category columns), chunking (per-page for
PDFs, character-budget for plain text), category suggestion (mocked model
calls, graceful no-key fallback), the book compiler's retrieval + error
handling for both providers, browser-supplied key resolution/priority, the
background upload-job lifecycle, the page-by-page review session's state
machine (approve/skip/retry/cancel, including recovering from a failed
page conversion without duplicating or losing work), the FastAPI endpoints
via `TestClient`, and the standalone chunked-upload script below.

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
library, not something to commit). Delete it to start fresh.

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
- Images (photos, handwriting kept as images by the PDF pipeline) are not
  stored or shown - only a text placeholder note. The store is optimised for
  searchable/compilable text content, not visual fidelity.
- The book compiler sends up to 20 matching chunks (8000 chars each) to the
  model per request - a very large personal library on a broad topic could
  exceed that and only see a partial set of matches.
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
