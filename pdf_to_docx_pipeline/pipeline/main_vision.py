"""
Same overall pipeline as main.py, but scanned pages go through a vision-LLM
call (vision_ocr.py) instead of local Tesseract. Stage 1 (born-digital text,
zero cost) is unchanged and still runs first - this only replaces the path
for pages that actually need OCR.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m pipeline.main_vision input.pdf output.docx --model claude-sonnet-5
"""
import argparse
import cv2
import numpy as np
import pymupdf as fitz

from . import extract, assemble, vision_ocr


def _crop_png(color_img: np.ndarray, bbox_px: tuple) -> bytes:
    l, t, r, b = bbox_px
    h, w = color_img.shape[:2]
    l, t = max(l, 0), max(t, 0)
    r, b = min(r, w), min(b, h)
    crop = color_img[t:b, l:r]
    ok, buf = cv2.imencode(".png", crop)
    if not ok or crop.size == 0:
        return None
    return buf.tobytes()


def run(pdf_path: str, output_path: str, model: str = vision_ocr.DEFAULT_MODEL,
        dpi: int = 200):
    blocks = extract.extract_pdf(pdf_path)

    doc_items = []
    stats = {"native_text_blocks": 0, "vision_text_blocks": 0,
             "handwritten_blocks": 0, "images": 0, "pages_sent_to_vision_model": 0}

    last_page = None
    for block in blocks:
        if block.page_number != last_page:
            doc_items.append({"kind": "heading", "page": block.page_number})
            last_page = block.page_number

        if block.kind == "text":
            doc_items.append({"kind": "text", "lang": "unknown", "text": block.text})
            stats["native_text_blocks"] += 1

        elif block.kind == "image":
            doc_items.append({"kind": "image", "image_bytes": block.image_bytes})
            stats["images"] += 1

        elif block.kind == "scanned_page":
            stats["pages_sent_to_vision_model"] += 1
            png_bytes = extract.render_page_for_ocr(pdf_path, block.page_number, dpi=dpi)
            color_img = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)

            model_blocks = vision_ocr.extract_page_with_vision(png_bytes, model=model)

            for b in model_blocks:
                if b["type"] == "text" and b.get("text"):
                    doc_items.append({"kind": "text", "lang": b.get("language", "unknown"),
                                       "text": b["text"]})
                    stats["vision_text_blocks"] += 1

                elif b["type"] == "photo":
                    crop = _crop_png(color_img, b["bbox_px"])
                    if crop:
                        doc_items.append({"kind": "image", "image_bytes": crop})
                        stats["images"] += 1

                elif b["type"] == "handwriting":
                    crop = _crop_png(color_img, b["bbox_px"])
                    if crop:
                        if b.get("text"):
                            # model made a real transcription attempt - keep
                            # both the image AND the transcription, clearly
                            # labelled as a draft, so a human can check it
                            # against the source without retyping from scratch
                            doc_items.append({"kind": "handwriting", "image_bytes": crop})
                            doc_items.append({"kind": "text", "lang": b.get("language", "unknown"),
                                               "text": f"(draft transcription) {b['text']}"})
                        else:
                            doc_items.append({"kind": "handwriting", "image_bytes": crop})
                        stats["handwritten_blocks"] += 1

    assemble.build_docx(doc_items, output_path)
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Digitise a mixed-content PDF into a .docx using a vision-LLM for scanned pages")
    parser.add_argument("input_pdf")
    parser.add_argument("output_docx")
    parser.add_argument("--model", default=vision_ocr.DEFAULT_MODEL,
                         help="claude-sonnet-5 (default), claude-haiku-4-5-20251001 (cheaper), "
                              "or claude-opus-4-8 (hardest pages)")
    parser.add_argument("--dpi", type=int, default=200,
                         help="Render DPI for scanned pages sent to the model")
    args = parser.parse_args()

    stats = run(args.input_pdf, args.output_docx, model=args.model, dpi=args.dpi)

    print(f"Wrote {args.output_docx}")
    print("--- run summary ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    total_pages = fitz.open(args.input_pdf).page_count
    print(f"  pages sent to the model: {stats['pages_sent_to_vision_model']} / {total_pages} total")


if __name__ == "__main__":
    main()
