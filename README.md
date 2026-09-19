# AjanDB

A personal knowledge-base system for digitising scanned/born-digital documents and making
them searchable, with a focus on bilingual English/Thai content.

## What it does

- Digitises PDFs — born-digital text extracted directly, scanned pages OCR'd (local
  Tesseract or an opt-in vision-LLM), handwriting preserved as a flagged image rather than
  guessed at.
- Stores everything as searchable markdown (SQLite + full-text search), one chunk per page,
  with a confidence score and review flag per chunk.
- Lets you review and correct a page before it's saved — low-confidence text and Thai
  spelling mistakes highlighted inline, with read-aloud and an opt-in second-model check.
- Synthesises a new book from stored content matching a topic, via Claude or GPT.

See [`CONTEXT.md`](CONTEXT.md) for architecture, design decisions, and known limitations.

## Setup

Two subprojects, each with its own environment — see their READMEs for exact commands:

- [`pdf_to_docx_pipeline/README.md`](pdf_to_docx_pipeline/README.md) — the digitisation
  engine (used standalone, or from the webapp below).
- [`webapp/README.md`](webapp/README.md) — the FastAPI + vanilla JS app (Data Store, Data
  Search, Data Generation).

## Run locally

```bash
cd webapp
uvicorn backend.main:app --reload --app-dir .
```

Then open http://127.0.0.1:8000/.

## Tests

```bash
cd webapp && python3 -m pytest tests/ -v
cd ../pdf_to_docx_pipeline && python3 -m pytest tests/ -v
```
