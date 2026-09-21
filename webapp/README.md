# AjanDB webapp

A FastAPI + vanilla JS front end for uploading unstructured files, storing them as
searchable markdown, and compiling a book from whatever matches a topic.

## What it does

- **Data Store** — upload `.pdf`/`.docx`/`.txt`/`.md`. PDFs go through
  `../pdf_to_docx_pipeline` with a choice of engine (free local OCR, or a per-page
  vision-LLM call for messy scans/non-Latin scripts).
- **Page-by-page review** (optional, PDF only) — approve or edit each page's converted
  text before it's saved. The scanned page and the editable text both size to fit your
  screen by default, and a shared **Zoom** control (50%-250%) magnifies both together for
  comparing fine detail. Thai spelling mistakes get a red wavy underline you can click for
  suggested corrections, word-processor style (no more scanning a summary list at the bottom
  to find the word). Optional **Second-model validation** cross-checks any page below 100%
  confidence against a second model/engine (plus an on-demand manual check, always available
  regardless of the toggle) and sets a "Needs review" badge — it doesn't paint the flagged
  text itself anymore, since that stopped being useful once a page had more than a couple of
  disagreements. Optional **Verify with model ensemble** goes further: every page needing
  OCR/vision is transcribed independently by both providers up front, with their disagreement
  spans (highlighted in the editor where they line up with what's shown, always shown in a
  dedicated comparison panel either way) as the primary review signal, instead of a one-off
  manual check per page — 2x the cost/latency of a scanned page, since it's two full
  transcriptions instead of one. Toggleable server-side read-aloud (Azure Speech) on top — see
  [`../CONTEXT.md`](../CONTEXT.md)'s "Multi-signal 'likely wrong' flagging", "Ensemble
  verification", and "Thai spell-check".
- **Data Search** — full-text search (SQLite FTS5) plus a book browser with per-book
  `.md` export, delete, a manual "ready for publish" flag, free-form **labels** (publish
  date, media type, content, author/publisher, or any custom key you add), **View/Export
  original** to get back the exact file a book was uploaded from, and (for a page-by-page
  review that was cancelled or abandoned partway through) a **Resume conversion** button that
  picks up right where it left off — see [`../CONTEXT.md`](../CONTEXT.md)'s "Managing stored
  books". Every book's original upload is now kept permanently for export, and a
  still-in-progress review also keeps a temporary copy so it can resume — unlike bulk
  uploads' converted markdown, which is all this app used to retain. Each page in a book's
  expanded "View pages" list has its own **Edit** button, so a page flagged "Needs review" -
  or any other saved page - can be corrected (and the flag cleared) without redoing the
  original page-by-page review.
- **Data Generation** — describe a topic and pick a mode: **Copy-paste** lists the exact
  matching content verbatim with a reference for each piece (no model call, nothing
  reworded), or **Generative** builds new material around it, keeping the original wording
  unchanged wherever it's quoted — as plain text, as one flow linking the same topic across
  different books, as a generated illustrative image, or as a short narrated vertical
  (TikTok-style) video — see [`../CONTEXT.md`](../CONTEXT.md)'s "Data Generation modes".
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
language packs, Noto fonts) — see that directory's README. Data Generation's **video** mode
additionally needs `ffmpeg` on the `PATH` (used to assemble the narrated video, not for OCR).

The Vision-LLM engine and Data Generation's text-based modes need an API key, either typed
into the page's "API Keys" panel (stored in that browser's `localStorage` only) or set in
the environment:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # for the Claude option
export OPENAI_API_KEY=sk-...          # for the GPT option
```

Data Generation's **image** and **video** modes always need an OpenAI key specifically
(Anthropic has no image-generation API) regardless of which provider the text modes are set
to, and **video** additionally needs an Azure Speech key/region (see below) to narrate the
script it writes.

Having *either* set is what lets page-by-page review's **Second-model validation** checkbox
(unchecked by default — see the review options above) actually do anything for any page
below 100% confidence, at the cost of an extra API call per such page when it's turned on —
this applies even to classical-engine (otherwise free) sessions, since the cross-check
always uses whichever of these two keys is available in the backend's environment.

Read Aloud (in page-by-page review, its own **Enable Read aloud** checkbox) and Data
Generation's **video** mode both need an Azure Speech key/region the same way — typed into
the "API Keys" panel, or:

```bash
export AZURE_SPEECH_KEY=...
export AZURE_SPEECH_REGION=eastus     # whichever region your Speech resource is in
```

Alternatively, `bash deploy.sh` installs system dependencies, creates the venv, and starts
the server for you, then opens a public Cloudflare tunnel on top of it — add `--no-tunnel`
to skip that and keep the server local-only (`http://localhost:8000/`), which sidesteps
tunnel connectivity issues entirely for local testing. See the script itself for the
`vercel` target and its current limitations.

**Docker** (deploy artifact for a future hosted target — build from the **repo root**,
not from `webapp/`, since the image needs `../pdf_to_docx_pipeline` too):

```bash
docker build -f webapp/Dockerfile -t ajandb .
docker run -p 8000:8000 -v ajandb-data:/app/webapp/data ajandb
```

The volume mount keeps `data/ajandb.sqlite3`, `data/images/`, `data/originals/`, and Data
Generation's `data/generated_images/`/`data/generated_videos/` across container restarts —
without it, the library (and anything generated) is wiped on every redeploy. There's no
persistent-volume or stable-URL config beyond this yet (that depends on which host is chosen
— see [`../CONTEXT.md`](../CONTEXT.md)'s "Not yet done").

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
