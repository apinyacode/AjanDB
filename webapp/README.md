# AjanDB webapp

A FastAPI + vanilla JS front end for uploading unstructured files, storing them as
searchable markdown, and compiling a book from whatever matches a topic.

## What it does

- **Data Store** — upload `.pdf`/`.docx`/`.txt`/`.md`. PDFs go through
  `../pdf_to_docx_pipeline` with a choice of engine (free local OCR, or a per-page
  vision-LLM call for messy scans/non-Latin scripts).
- **Page-by-page review** (optional, PDF only) — approve or edit each page's converted
  text before it's saved, with low-confidence text and Thai spelling flagged inline,
  read-aloud, and an opt-in second-model cross-check.
- **Data Search** — full-text search (SQLite FTS5) plus a book browser with per-book
  `.md` export.
- **Data Generation** — synthesises a new markdown book from stored content matching a
  topic, via Claude or GPT.
- **`scripts/chunked_upload.py`** — ingests a large book outside the browser, with resume
  support for interrupted runs.

See [`../CONTEXT.md`](../CONTEXT.md) for architecture, design decisions, data storage
details, and known limitations.

## Setup

```bash
cd webapp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

PDF uploads need the same system dependencies as `../pdf_to_docx_pipeline` (Tesseract +
language packs, Noto fonts) — see that directory's README.

The Vision-LLM engine and Data Generation need an API key, either typed into the page's
"API Keys" panel (stored in that browser's `localStorage` only) or set in the environment:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # for the Claude option
export OPENAI_API_KEY=sk-...          # for the GPT option
```

Alternatively, `bash deploy.sh` installs system dependencies, creates the venv, and starts
the server for you — see `deploy.sh --help` and the script itself for the `vercel` target
and its current limitations.

**Docker** (deploy artifact for a future hosted target — build from the **repo root**,
not from `webapp/`, since the image needs `../pdf_to_docx_pipeline` too):

```bash
docker build -f webapp/Dockerfile -t ajandb .
docker run -p 8000:8000 -v ajandb-data:/app/webapp/data ajandb
```

The volume mount keeps `data/ajandb.sqlite3` and `data/images/` across container restarts
— without it, the library is wiped on every redeploy. There's no persistent-volume or
stable-URL config beyond this yet (that depends on which host is chosen — see
[`../CONTEXT.md`](../CONTEXT.md)'s "Not yet done").

## Run locally

```bash
cd webapp
uvicorn backend.main:app --reload --app-dir .
```

Then open http://127.0.0.1:8000/ — the backend serves the frontend directly.

## Tests

```bash
cd webapp
python3 -m pytest tests/ -v
```
