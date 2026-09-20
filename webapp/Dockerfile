# Build from the REPO ROOT, not from webapp/:
#   docker build -f webapp/Dockerfile -t ajandb .
# webapp/backend/convert.py locates ../pdf_to_docx_pipeline relative to its
# own file path (see its _REPO_ROOT/_PIPELINE_DIR), so both directories must
# land in the image at the same relative layout they have in the repo.
FROM python:3.11-slim

# Same system packages deploy.sh's deploy_local() apt-installs for the local
# target: Tesseract (+ English/Thai/Chinese language packs) for the
# classical OCR engine, ffmpeg to assemble Data Generation's video mode
# (see content_studio.py).
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-tha tesseract-ocr-chi-sim \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install both subprojects' Python dependencies before copying source, so
# an edit to application code doesn't invalidate this layer.
COPY pdf_to_docx_pipeline/requirements.txt pdf_to_docx_pipeline/requirements.txt
COPY webapp/requirements.txt webapp/requirements.txt
RUN pip install --no-cache-dir \
    -r pdf_to_docx_pipeline/requirements.txt \
    -r webapp/requirements.txt

COPY pdf_to_docx_pipeline/ pdf_to_docx_pipeline/
COPY webapp/backend/ webapp/backend/
COPY webapp/frontend/ webapp/frontend/

EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "webapp"]
