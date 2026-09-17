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
  of silently passing as fact.
- **Data Search** — full-text keyword search (SQLite FTS5) over every stored
  chunk, returning matches with their filename, page number, category,
  confidence, review flag, and a highlighted snippet.
- **Data Generation** — give it an instruction (e.g. "compile a book from all
  the files containing content about how to raise a child"); it keyword-
  searches the store for matching chunks and asks Claude or GPT (your choice,
  per request) to synthesise them into one coherent book in markdown.

## Deploy / redeploy (one command)

```bash
bash webapp/deploy.sh
```

Installs any missing system dependencies (git, Tesseract + language packs,
ffmpeg), creates the venv on first run, installs/updates Python packages,
pulls the latest code (skipped automatically if you have uncommitted local
changes - it never discards work), restarts the server, and opens a public
`cloudflared` tunnel. Safe to re-run any time; every step is idempotent.
Ctrl+C stops both the tunnel and the server.

Use `bash webapp/deploy.sh --no-pull` to redeploy what's already on disk
without touching git (useful right after editing something locally).

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

87 tests total (37 in the pipeline, 50 here) covering the SQLite/FTS5 layer
(including confidence/needs_review/category columns), chunking (per-page for
PDFs, character-budget for plain text), category suggestion (mocked model
calls, graceful no-key fallback), the book compiler's retrieval + error
handling for both providers, browser-supplied key resolution/priority, and
the FastAPI endpoints via `TestClient`.

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
