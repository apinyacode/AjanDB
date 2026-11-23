# Build from the REPO ROOT, not from webapp/:
#   docker build -f webapp/Dockerfile -t ajandb .
#   docker run -p 8000:8000 -v ajandb-data:/app/webapp/data -e AJANDB_PASSWORD=... ajandb
# webapp/backend/convert.py locates ../pdf_to_docx_pipeline relative to its
# own file path (see its _REPO_ROOT/_PIPELINE_DIR), so both directories must
# land in the image at the same relative layout they have in the repo.
# On Render (render.yaml at the repo root) the same image runs with a
# persistent disk at /app/webapp/data - the database, page images, originals
# and QnA scans all live there - and Render's PORT setting.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Same system packages deploy.sh's deploy_local() apt-installs for the local
# target: Tesseract (+ English/Thai/Chinese language packs) for the
# classical OCR engine, ffmpeg to assemble Data Generation's video mode
# (see content_studio.py). Noto fonts are for the test suite run below
# (make_test_pdf.py draws its Thai/Latin test pages with them).
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-tha tesseract-ocr-chi-sim \
    ffmpeg fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install both subprojects' Python dependencies before copying source, so
# an edit to application code doesn't invalidate this layer. Dev
# requirements too (pytest, httpx), for the test run below.
COPY pdf_to_docx_pipeline/requirements.txt pdf_to_docx_pipeline/requirements.txt
COPY webapp/requirements.txt webapp/requirements-dev.txt webapp/
RUN pip install --no-cache-dir \
    -r pdf_to_docx_pipeline/requirements.txt \
    -r webapp/requirements-dev.txt

COPY pdf_to_docx_pipeline/ pdf_to_docx_pipeline/
COPY webapp/backend/ webapp/backend/
COPY webapp/frontend/ webapp/frontend/
COPY webapp/scripts/ webapp/scripts/
COPY webapp/tests/ webapp/tests/

# A change that breaks the tests fails the build here instead of going live -
# Render keeps the previous version running (same as FarmDeApp's Dockerfile).
RUN cd webapp && python -m pytest -q -p no:cacheprovider \
    && cd ../pdf_to_docx_pipeline && python -m pytest -q -p no:cacheprovider

ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)"
# One worker on purpose: upload/QnA jobs and review sessions live in this
# process's memory (see main.py's _jobs and review.py's _sessions).
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --app-dir webapp"]
