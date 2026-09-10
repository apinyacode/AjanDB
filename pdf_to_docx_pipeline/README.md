# PDF → DOCX digitisation pipeline (low-power, mostly non-AI)

Turns a mixed-content PDF (typed text in multiple languages, printed photos/figures,
and handwritten notes) into a `.docx`, using classical scripting and small local
models wherever possible, and treating any "AI" step as an expensive resource to
spend only where nothing else works.

## Why this shape

The three content types in your PDFs need genuinely different treatment, and
conflating them is how projects end up sending every page through an expensive
model "just in case":

| Content | Best approach | Compute cost |
|---|---|---|
| Typed text in a born-digital PDF | Read the PDF's own text objects directly | ~zero — no image processing, no model, no OCR at all |
| Typed/printed text in a scanned image | Classical image cleanup + local Tesseract OCR | Small — CPU-only, sub-second per page, no GPU, no network call |
| Handwriting | No non-ML method reliably transcribes it | Zero, by keeping it as an embedded image instead of guessing |

That last row is the honest part of this design. There is no classical (non-machine-learning)
technique that reliably reads handwriting — every accurate handwriting engine (Kraken,
Calamari, PyLaia, TrOCR, commercial cloud OCR APIs) is a trained neural network, because
handwriting doesn't reduce to a small fixed set of glyph shapes the way print does. Given
your stated priority — correct digitisation *and* minimal environmental impact — the
right default isn't "run a big model on it anyway," it's: **don't guess. Preserve the
handwriting as an image, flagged for a human to transcribe**, and treat OCR-based
transcription as an optional, clearly-labelled second pass you turn on deliberately.

## Pipeline stages

```
PDF
 │
 ├─ Stage 1 (extract.py)     Does this page have a real text layer?
 │                             YES → read text + images directly (zero OCR)
 │                             NO  → rasterize page, hand to Stage 2
 │
 ├─ Stage 2 (ocr.py)          Classical OpenCV cleanup (deskew, denoise,
 │                            binarize) → local Tesseract 5 OCR, multi-language
 │
 ├─ Stage 3 (handwriting.py)  Classical stroke-geometry check on every OCR'd
 │                            line AND every embedded image: does it look
 │                            handwritten? (No ML — stroke-width variance +
 │                            baseline-wander, both plain image processing.)
 │
 └─ Stage 4 (assemble.py)     Rebuild the document in original reading order:
                              real text → paragraph (language-tagged);
                              picture → embedded image;
                              handwriting → embedded image + review flag
```

## What's genuinely "non-AI" here, and what isn't

Being precise about this, since it's central to the brief:

- **Zero-AI, always:** born-digital text extraction (Stage 1), all OpenCV
  preprocessing (deskew/denoise/binarize), the stroke-geometry and
  baseline-geometry handwriting heuristics, Thai spacing cleanup, and the
  whole `.docx` assembly. None of this touches a model of any kind.
- **A small local model, not an LLM:** Tesseract 5's recognition step is
  technically an LSTM neural network internally — it stopped being pure
  pattern-matching around Tesseract 4. It's genuinely tiny compared to a
  vision-language model: it runs on CPU in well under a second per page,
  needs no GPU, no network call, and no per-token billing. This is the
  deliberate trade in the whole design — a small local model instead of a
  large remote one, only for the pages that actually need it (scanned
  pages without a text layer).
- **Deliberately not used:** any cloud OCR API, any vision-LLM call. If you
  later want an attempt at transcribing the flagged handwriting, the
  natural next step is **Kraken** (`kraken.re`) — also a small, local,
  CPU-friendly model, not a cloud LLM — but its pretrained models are
  tuned mostly for historical/Latin manuscripts, so treat its output on
  modern mixed-script handwriting as a rough draft to proofread, not a
  final answer. This is left as an opt-in module, not wired in by default.

## Environmental reasoning

The biggest lever isn't which OCR engine you pick — it's how many pages
actually need OCR at all. A pipeline that runs everything through OCR "to be
safe" spends compute on pages that already had perfectly good, free text
available. Stage 1's job is to make that waste visible: the CLI prints how
many pages needed OCR versus how many didn't, so you can see the actual
computational footprint of a given batch, not just assume it.

Within the pages that do need OCR, Tesseract on CPU is about as
power-light as automated text recognition gets — no GPU inference, no
data leaving the machine, no cloud infrastructure spun up per request.
That's the ceiling on how "AI-light" this can be while still handling
scanned pages at all.

## Known limitations (tested, not just claimed)

These came from actually running the pipeline against a synthetic test PDF
with mixed English/Thai text, a scanned page, a photo, and a simulated
handwritten note — not just described in the abstract:

- **Short-text language tagging can misfire.** `langdetect` is a
  statistical n-gram model and it occasionally misclassifies short titles
  (a 6-word English heading was tagged as German in testing). It's fine on
  full sentences and gets shakier under ~10 words. If your documents have a
  lot of short headings, consider a fixed default language with per-block
  override rather than trusting auto-detection on every fragment.
- **Thai OCR has real, expected error rates.** Tesseract's Thai model
  inserts a space between almost every character (Thai doesn't space
  between words at all) - this pipeline includes a regex cleanup for
  that (`normalize_thai_spacing`), but individual character misreads still
  happen (e.g. a tone-mark vowel occasionally misread), same as any OCR
  engine on non-Latin scripts. Expect to proofread OCR'd Thai, especially
  from lower-quality scans.
- **The handwriting heuristic is a heuristic, not a classifier trained on
  your documents.** It combines stroke-width variance and baseline wander,
  and needed real tuning against test data to avoid false positives -
  scripts that stack diacritics/tone marks above or below the letter body
  (Thai, Vietnamese, Arabic, Devanagari) show naturally more baseline
  spread than Latin print, so it requires *both* geometric irregularity
  *and* low OCR confidence before flagging a line as handwriting, rather
  than trusting geometry alone. Before trusting it unattended on your real
  documents, run it on a handful of representative pages and check the
  `handwritten_lines` count against what you'd expect by eye - `--handwriting-threshold`
  is exposed on the CLI for tuning.
- **Reading order isn't always perfect.** For unusual multi-column layouts,
  PyMuPDF's `sort=True` (used here) helps but isn't perfect - very complex
  layouts may need manual bounding-box logic on top.

## Two engines: classical (main.py) vs vision-LLM (main_vision.py)

Real testing against a mixed Thai/English scanned book surfaced a genuine
limit of the classical approach: separating "photo" from "dense non-Latin
text" by pixel geometry alone gets unreliable on messy real-world scans
(halftone print textures in particular). `main.py` includes a safety net for
this (a confidence floor that keeps uncertain regions as images rather than
publishing garbage text), which is honest and safe, but on a document that
hits this limit heavily, a lot of content ends up parked as "needs review"
images instead of searchable text.

`main_vision.py` is the same pipeline with scanned pages routed through a
Claude vision call instead of Tesseract. A vision-language model separates
text/photo/handwriting semantically rather than by pixel statistics, and
reads non-Latin scripts more robustly than a small local OCR model - so it's
the better tool specifically for documents like this. The trade-off is real
and worth being explicit about: this calls a cloud model per page, which
costs tokens and real energy, which is the opposite of the "minimal
footprint" framing this project started with.

**Recommended policy:** try `main.py` first - it's free and often good
enough, especially for clean typed documents. Fall back to
`main_vision.py` for documents (or specific pages) where the classical
engine's `low_confidence_regions` / `handwritten_lines` counts come back
high, rather than defaulting to the vision engine for everything.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 -m pipeline.main_vision input.pdf output.docx --model claude-sonnet-5
```

`--model` also accepts `claude-haiku-4-5-20251001` (cheaper/faster, fine
for straightforward pages) or `claude-opus-4-8` (for the hardest
handwriting-heavy pages).

Important honesty note: `main_vision.py` has been tested for structural
correctness (JSON parsing, bbox-to-pixel-crop math, docx assembly) using a
mocked model response, but **not** run end-to-end against a real API key in
the environment this was built in - there's no key available there. Run it
on a few pages first and check the output before trusting it on a full
document.



```bash
# one-time setup
pip install pymupdf opencv-python-headless python-docx langdetect pytesseract
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-tha  # + any other language packs you need

python3 -m pipeline.main input.pdf output.docx --langs eng+tha
```

Flags:
- `--langs` — Tesseract language codes, `+`-separated (e.g. `eng+tha+chi_sim`).
  Install the matching `tesseract-ocr-<code>` package for each one.
- `--handwriting-threshold` — lower this to flag more lines as handwriting
  (more cautious, more manual review), raise it to flag fewer (trust OCR more).

The run prints a summary: how many blocks came from the free text-layer path,
how many pages needed OCR, and how many lines/images were flagged as
handwriting — use this as your environmental-footprint readout per batch.

## Files

- `pipeline/extract.py` — Stage 1: born-digital text/image extraction
- `pipeline/ocr.py` — Stage 2: preprocessing + Tesseract OCR + Thai spacing fix
- `pipeline/handwriting.py` — Stage 3: classical handwriting heuristic
- `pipeline/assemble.py` — Stage 4: `.docx` assembly
- `pipeline/main.py` — CLI orchestrator (classical engine)
- `pipeline/vision_ocr.py`, `pipeline/main_vision.py` — vision-LLM engine
- `make_test_pdf.py` — generates the synthetic test PDF used to validate all of the above
- `tests/` — pytest suite (see below)

## Testing

```bash
# one-time setup
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-tha tesseract-ocr-chi-sim
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python3 -m pytest tests/ -v
```

The suite covers each stage in isolation (text-layer detection, Thai spacing
cleanup, language tagging, the handwriting heuristic, `.docx` assembly) plus
end-to-end runs of both `main.py` and `main_vision.py` against a synthetic
test PDF. `main_vision.py`'s tests mock the Anthropic client — no
`ANTHROPIC_API_KEY` is required to run the suite, consistent with the
honesty note above about it not having been run against a real key.

The test PDF isn't checked in — `tests/conftest.py` builds it on the fly via
`make_test_pdf.build()` (deterministic, fixed random seed) so the repo
doesn't carry large binary fixtures. `make_test_pdf.py` looks for Noto
fonts at `/usr/share/fonts/truetype/noto` by default, or `$TEST_PDF_FONT_DIR`
if set — e.g. `apt-get install fonts-noto-core fonts-noto-cjk` on
Debian/Ubuntu. Run `python3 make_test_pdf.py` to generate a local copy of
the input PDF, then `python3 -m pipeline.main sample_input.pdf out.docx
--langs eng+tha` to see a real example output for yourself.
