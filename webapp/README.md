# AjanDB webapp

A simple front end + backend for uploading unstructured files, storing them
as searchable markdown, and compiling a book from whatever matches a topic.

- **Upload** — .pdf, .docx, .txt, or .md. PDFs go through the digitisation
  pipeline in `../pdf_to_docx_pipeline` (born-digital text extraction, local
  OCR for scanned pages, handwriting detection); everything else is read
  directly. The result is stored as markdown in SQLite.
- **Search** — full-text keyword search (SQLite FTS5) over every stored
  file's markdown, returning the matching files with a highlighted snippet.
- **Compile a book** — give it an instruction (e.g. "compile a book from all
  the files containing content about how to raise a child"); it keyword-
  searches the store for matching files and asks Claude to synthesise them
  into one coherent book in markdown.

## Setup

```bash
cd webapp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

PDF uploads need the same system dependencies as `pdf_to_docx_pipeline`
(Tesseract + language packs, Noto fonts) - see that directory's README.

## Run

```bash
uvicorn backend.main:app --reload --app-dir .
```

Then open http://127.0.0.1:8000/ - the backend serves the frontend directly,
so there's nothing separate to run for the UI.

The "Compile a book" tab requires `ANTHROPIC_API_KEY` to be set in the
backend's environment:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Without it, upload and search work fully; compiling returns a clear error
rather than crashing (tested; the actual model call is mocked in the test
suite since no key is available in this environment).

## Test

```bash
python3 -m pytest tests/ -v
```

22 tests covering the SQLite/FTS5 layer, file-type conversion (including an
end-to-end PDF-to-markdown run through the real digitisation pipeline), the
book compiler's retrieval + error handling (mocked model calls), and the
FastAPI endpoints via `TestClient`.

## Data storage

Everything lands in `data/ajandb.sqlite3` (gitignored - it's your local
library, not something to commit). Delete it to start fresh.

## Known limitations

- Search/retrieval for the book compiler is keyword-based (SQLite FTS5), not
  semantic - it finds files containing your topic's words, not files that
  are conceptually related without using them. Fine for a personal library
  at moderate scale; a vector index would be the natural upgrade if keyword
  search starts missing relevant files.
- Images (photos, handwriting kept as images by the PDF pipeline) are not
  stored or shown - only a text placeholder note. The store is optimised for
  searchable/compilable text content, not visual fidelity.
- The book compiler sends up to 20 matching files (8000 chars each) to the
  model per request - a very large personal library on a broad topic could
  exceed that and only see a partial set of matches.
