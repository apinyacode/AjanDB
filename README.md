# AjanDB

A knowledge-base web app for digitising scanned and born-digital documents (books, field
notes, handwritten question slips) and making them searchable, with a focus on bilingual
Thai/English content. Built as a prototype for a small group of users.

> **New to this repo?** Read this file top to bottom, then [`CONTEXT.md`](CONTEXT.md) - the
> detailed architecture, design decisions and known limitations. The section
> [Continue the work with Claude](#continue-the-work-with-claude) explains how to pick the
> project up with your own Claude account.

## What it does

The web app has four tabs:

| Tab | What it's for |
|---|---|
| **1. Data Store** | Upload `.pdf`/`.docx`/`.txt`/`.md`. PDFs are digitised (born-digital text read directly; scanned pages OCR'd with free local Tesseract or a paid vision model). Optional **page-by-page review**: the scan next to editable text, with line numbers, Thai spelling suggestions, read-aloud, and second-model checks. |
| **2. Data Search** | Full-text search, and a book list with labels, "ready for publish", resume of unfinished reviews, and exports: **.md**, **draft .docx** (any time), **.docx** (only when every page is reviewed and the book is marked ready for publish), and the original file. |
| **3. Data Generation** | Build new material from stored content on a topic: verbatim copy-paste with references, or generated text, cross-book "flow", an image, or a short narrated video. |
| **4. QnA** | Handwritten question slips (Thai/English): Claude transcribes every page, a person checks each page against its scan (spell-check, unsure words highlighted) and marks it done; export to CSV or Word. |

Access for testers is a single shared password (`AJANDB_PASSWORD`); there are no individual
user accounts.

## Repository layout

```
AjanDB/
├── README.md                 this file - start here
├── CONTEXT.md                architecture, decisions, limitations, current state (the long read)
├── CLAUDE.md                 instructions Claude Code loads automatically in this repo
├── PROJECT_TEMPLATE.md       the README/CONTEXT conventions this project follows
├── render.yaml               optional Render hosting config (see "Deploying")
├── pdf_to_docx_pipeline/     the digitisation engine (extract → OCR → handwriting → assemble)
│   └── README.md
└── webapp/                   FastAPI backend + plain HTML/CSS/JS frontend (no build step)
    ├── backend/              API (main.py), storage (db.py), conversion, review, QnA, exports...
    ├── frontend/             index.html, app.js, style.css
    ├── tests/                pytest suite (~300 tests)
    ├── scripts/              command-line helpers (large-book upload, QnA import)
    ├── deploy.sh             one-command local run / public sharing with a tunnel
    ├── Dockerfile            production image (runs the test suite while building)
    └── README.md             detailed setup, features and deployment notes
```

Everything the app stores - the SQLite database, page images, original uploads, QnA scans -
lives in `webapp/data/`, which is **not** in git.

## Get it running

You need Linux or macOS (on Windows, use WSL), Python 3.10+, and git.

**Easiest - the deploy script** (installs system packages such as Tesseract and ffmpeg, sets
up a Python environment, and starts the server):

```bash
git clone https://github.com/apinyacode/AjanDB.git
cd AjanDB/webapp
bash deploy.sh --no-tunnel
```

Open http://localhost:8000 (on a Chromebook, http://penguin.linux.test:8000 if localhost
doesn't load). The first start after installing takes about a minute.

**By hand**, see [`webapp/README.md`](webapp/README.md#setup):

```bash
cd webapp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt -r ../pdf_to_docx_pipeline/requirements.txt
uvicorn backend.main:app --reload --app-dir .
```

**API keys** are never stored in the repo. Either type them into the app's **API Keys** panel
(kept only in that browser), or put them in `webapp/.env` (git-ignored):

```
ANTHROPIC_API_KEY=sk-ant-...   # QnA, the vision OCR engine, Data Generation with Claude
OPENAI_API_KEY=sk-...          # the GPT options, image/video generation
AJANDB_PASSWORD=...            # optional: require this password (any username) for everything
```

## Run the tests

```bash
cd webapp && .venv/bin/python -m pytest tests/ -q
cd ../pdf_to_docx_pipeline && ../webapp/.venv/bin/python -m pytest tests/ -q
```

All tests should pass. They need Tesseract (with the Thai language pack), ffmpeg and the Noto
fonts installed - `deploy.sh` installs the first two; on Ubuntu/Debian add the fonts with
`sudo apt-get install fonts-noto-core`. No test makes a paid API call: Claude, OpenAI and
Azure are always mocked.

## Deploying and sharing

| Option | Cost | Status |
|---|---|---|
| **Tunnel from your own computer** - `bash deploy.sh` (without `--no-tunnel`) | Free | **What's used today.** Prints a public `https://…trycloudflare.com` link and a tester password. Works only while that computer is on and the terminal open; the link changes each run. |
| **Render** - `render.yaml` | ~US$28/month (Standard plan 2 GB + disk) | Config ready, **no service created yet**. Click-by-click steps in [`webapp/README.md`](webapp/README.md#deploy-online-with-render). |
| **A small Linux server** (AWS Lightsail, Azure VM, Hetzner...) | ~US$5-40/month | Not set up. Needs ≥2 GB RAM, a normal disk (not a network file share - SQLite), and the steps in `deploy.sh` plus a process manager and HTTPS. |

Sizing notes: converting one large PDF peaks at ~625 MB of memory, so 512 MB plans are too
small; ~100 books need 3-5 GB of disk. Keep **one** server instance - SQLite and the in-memory
upload/review jobs don't work across several.

## How the work is organised

- **Branches and pull requests.** Work on a branch, open a pull request into `main`, merge
  when the tests pass. (Once a Render service is created from `render.yaml`, every merge into
  `main` deploys automatically.)
- **Tests first-class.** Every change adds or updates tests; the Docker build runs the whole
  suite, so a breaking change can't be deployed.
- **`CONTEXT.md` is kept current in the same change** - a feature isn't done until its
  section there (and the README, if user-visible) says what it does and why.
- **Checked in a real browser.** User-facing changes are clicked through in headless Chromium
  (Playwright) before merging, not only unit-tested.
- **Commit messages:** what changed, in the imperative, then why - see
  [`PROJECT_TEMPLATE.md`](PROJECT_TEMPLATE.md).
- **Open threads and limitations** are listed at the end of [`CONTEXT.md`](CONTEXT.md)
  ("Known limitations" and "Not yet done / natural next steps").

## Continue the work with Claude

This project was built with Claude Code, and the repo is set up so the next developer's
Claude can carry on: Claude Code automatically reads [`CLAUDE.md`](CLAUDE.md), which points it
at `CONTEXT.md` and the conventions above.

### 1. Get access to the repository (current owner does this)

On GitHub, open **apinyacode/AjanDB → Settings**, then either:

- **Collaborators → Add people** → the new developer's GitHub username → **Write** access
  (they accept the email invitation), or
- **General → Danger Zone → Transfer ownership**, to hand the repository over completely.

The repository is public, so anyone can read and clone it; pushing changes needs one of the
above.

### 2a. Use Claude Code in the browser (no installation)

1. Sign in at **https://claude.ai** with a Claude plan that includes Claude Code.
2. Open **https://claude.ai/code** and connect GitHub at
   **https://claude.ai/connect-github** when asked.
3. If asked, install the **Claude GitHub App** on the repository (or on the account that
   owns it) from that same page, and give it access to **AjanDB**.
4. Start a new session with the **AjanDB** repository selected, and begin with something like:

   > Read CONTEXT.md and give me a summary of where the project stands and what's open.

Claude works on a branch in a cloud container and opens pull requests for you to review
and merge.

### 2b. Or use Claude Code on your own computer

```bash
curl -fsSL https://claude.ai/install.sh | bash     # installs Claude Code
git clone https://github.com/apinyacode/AjanDB.git
cd AjanDB
claude                                             # first run asks you to sign in
```

Running locally lets Claude use your own installed tools and start the app on your machine
(`bash webapp/deploy.sh --no-tunnel`).

### Things that don't come with the repo

- **API keys** - each developer uses their own (Anthropic console, OpenAI, Azure Speech).
- **Data** - books, QnA scans and the database live in `webapp/data/` on whichever machine
  ran the app. Hand them over separately (and carefully: the QnA material includes private
  letters) by copying that folder, or start empty.
- **The tester password** - kept in `webapp/.env` on the machine that runs the tunnel.
