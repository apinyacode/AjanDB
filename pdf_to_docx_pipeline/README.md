# PDF → DOCX digitisation pipeline (low-power, mostly non-AI)

Turns a mixed-content PDF (typed text in multiple languages, printed photos/figures, and
handwritten notes) into a `.docx`, using classical scripting and small local models wherever
possible, treating any vision-LLM step as an expensive resource spent only where nothing
else works.

## What it does

- Extracts born-digital text directly from the PDF's own text layer — zero OCR, zero
  compute cost, whenever a page already has one.
- Runs local Tesseract OCR (classical engine, `main.py`) on scanned pages with no text
  layer, with classical OpenCV preprocessing (deskew/denoise/binarize) first.
- Flags likely handwriting via a classical stroke-geometry heuristic (no ML) and preserves
  it as an embedded image rather than guessing at a transcription.
- Offers a vision-LLM engine (`main_vision.py`, Claude or GPT-4o) as an opt-in alternative
  for messy real-world scans and non-Latin scripts, at the cost of tokens and an API key.
- Rebuilds the document in original reading order as a `.docx`, with real text as
  paragraphs and pictures/handwriting as embedded images.

See [`../CONTEXT.md`](../CONTEXT.md) for the pipeline's design rationale, the two-engine
policy, and known limitations.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-tha  # + any other language packs you need
```

## Run locally

```bash
python3 -m pipeline.main input.pdf output.docx --langs eng+tha
```

Or, using the vision-LLM engine instead:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY for --provider openai
python3 -m pipeline.main_vision input.pdf output.docx --provider anthropic --model claude-sonnet-5
```

Run `python3 -m pipeline.main --help` / `python3 -m pipeline.main_vision --help` for the
full flag list (languages, handwriting sensitivity, model choice).

## Tests

```bash
python3 -m pytest tests/ -v
```

No `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` is required — `main_vision.py`'s tests mock the
model client. The test PDF isn't checked in; `tests/conftest.py` builds it on the fly via
`make_test_pdf.build()`, which looks for Noto fonts at `/usr/share/fonts/truetype/noto` by
default (override with `$TEST_PDF_FONT_DIR`).

## Files

- `pipeline/extract.py` — Stage 1: born-digital text/image extraction
- `pipeline/ocr.py` — Stage 2: preprocessing + Tesseract OCR + Thai spacing fix
- `pipeline/handwriting.py` — Stage 3: classical handwriting heuristic
- `pipeline/assemble.py` — Stage 4: `.docx` assembly
- `pipeline/main.py` — CLI orchestrator (classical engine)
- `pipeline/vision_ocr.py`, `pipeline/main_vision.py` — vision-LLM engine
- `make_test_pdf.py` — generates the synthetic test PDF used to validate all of the above
- `tests/` — pytest suite
