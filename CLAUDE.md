# Working on AjanDB

Read [`CONTEXT.md`](CONTEXT.md) before changing anything - it is the standing reference for
architecture, design decisions, known limitations and what's still open. [`README.md`](README.md)
covers setup, running, testing and deployment.

Conventions (details in `CONTEXT.md` → "Conventions and patterns"):

- Work on a branch and open a pull request into `main`; don't push to `main` directly.
- Add or update tests with every change; run both suites before calling something done
  (`webapp/tests/` and `pdf_to_docx_pipeline/tests/`). Never make paid API calls in tests -
  mock Claude, OpenAI and Azure, as the existing tests do.
- Check user-facing changes in a real browser (headless Chromium via Playwright), not only
  with unit tests.
- Update `CONTEXT.md` (and `README.md`/`webapp/README.md` when it's user-visible) in the same
  change, including "Current state" and "Known limitations".
- Comments explain non-obvious *why*, never restate *what*; keep abstractions minimal.
- Never commit secrets or anything under `webapp/data/` (it holds users' documents, including
  private letters in QnA).
