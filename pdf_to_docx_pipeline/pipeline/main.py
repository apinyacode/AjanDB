"""
End-to-end pipeline: PDF -> .docx.

Usage:
    python -m pipeline.main input.pdf output.docx [--langs eng+tha+chi_sim]

Design summary (see README.md for the full reasoning):
  1. Try to read each page's real text layer first (extract.py). Zero OCR,
     zero meaningful power draw, perfect accuracy - this is the path most
     modern PDFs (anything exported from Word, Google Docs, Canva, a phone
     scanning app that already OCR'd, etc.) can use entirely.
  2. Only pages with no text layer are rasterized and sent through local
     Tesseract OCR, after classical (non-ML) OpenCV preprocessing (ocr.py).
  3. Within OCR'd pages, lines are screened with a classical stroke-geometry
     heuristic (handwriting.py) to catch handwriting. Flagged lines are kept
     as images rather than mistranscribed as text.
  4. Everything is assembled back into one .docx in original reading order,
     with a small language tag on every text block (assemble.py).
"""
import argparse
import sys
import pymupdf as fitz

from . import extract, ocr, handwriting, assemble


def run(pdf_path: str, output_path: str, langs: str = ocr.DEFAULT_LANGS,
        handwriting_threshold: float = 0.85):
    blocks = extract.extract_pdf(pdf_path)

    doc_items = []
    stats = {"native_text_blocks": 0, "ocr_lines": 0,
             "handwritten_lines": 0, "images": 0, "ocr_pages": 0,
             "low_confidence_regions": 0}

    last_page = None
    for block in blocks:
        if block.page_number != last_page:
            doc_items.append({"kind": "heading", "page": block.page_number})
            last_page = block.page_number

        if block.kind == "text":
            lang = ocr.tag_language(block.text)
            doc_items.append({"kind": "text", "lang": lang, "text": block.text})
            stats["native_text_blocks"] += 1

        elif block.kind == "image":
            label = handwriting.classify_embedded_image(
                block.image_bytes, threshold=handwriting_threshold)
            if label == "handwritten":
                doc_items.append({"kind": "handwriting", "image_bytes": block.image_bytes})
                stats["handwritten_lines"] += 1
            else:
                doc_items.append({"kind": "image", "image_bytes": block.image_bytes})
                stats["images"] += 1

        elif block.kind == "scanned_page":
            stats["ocr_pages"] += 1
            png_bytes = extract.render_page_for_ocr(pdf_path, block.page_number)
            result = ocr.process_scanned_page(png_bytes, langs=langs)
            lines, processed_img = result["lines"], result["processed"]

            for pic_bytes in result["pictures"]:
                pic_label = handwriting.classify_embedded_image(
                    pic_bytes, threshold=handwriting_threshold)
                if pic_label == "handwritten":
                    doc_items.append({"kind": "handwriting", "image_bytes": pic_bytes})
                    stats["handwritten_lines"] += 1
                else:
                    doc_items.append({"kind": "image", "image_bytes": pic_bytes})
                    stats["images"] += 1

            for line in lines:
                if line["confidence"] < 40:
                    # Low enough that this almost certainly isn't clean
                    # printed text Tesseract actually read correctly - could
                    # be photo/texture noise, a damaged scan, or genuinely
                    # hard handwriting. Either way, publishing it as text
                    # would be worse than the image itself: garbage
                    # characters look like real (wrong) data, whereas an
                    # image is honestly just "not yet transcribed".
                    crop = assemble.crop_to_png_bytes(processed_img, line["bbox"])
                    doc_items.append({"kind": "uncertain", "image_bytes": crop})
                    stats["low_confidence_regions"] += 1
                    continue
                label = handwriting.classify_line(
                    processed_img, line["bbox"], line["confidence"],
                    threshold=handwriting_threshold,
                )
                if label == "handwritten":
                    crop = assemble.crop_to_png_bytes(processed_img, line["bbox"])
                    doc_items.append({"kind": "handwriting", "image_bytes": crop})
                    stats["handwritten_lines"] += 1
                else:
                    lang = ocr.tag_language(line["text"])
                    doc_items.append({"kind": "text", "lang": lang, "text": line["text"]})
                    stats["ocr_lines"] += 1

    assemble.build_docx(doc_items, output_path)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Digitise a mixed-content PDF into a .docx")
    parser.add_argument("input_pdf")
    parser.add_argument("output_docx")
    parser.add_argument("--langs", default=ocr.DEFAULT_LANGS,
                         help="Tesseract language codes, '+' separated, e.g. eng+tha+chi_sim")
    parser.add_argument("--handwriting-threshold", type=float, default=0.85,
                         help="Stroke-irregularity cutoff; lower = flags more lines as handwriting")
    args = parser.parse_args()

    stats = run(args.input_pdf, args.output_docx, langs=args.langs,
                handwriting_threshold=args.handwriting_threshold)

    print(f"Wrote {args.output_docx}")
    print("--- run summary ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    total_pages = fitz.open(args.input_pdf).page_count
    print(f"  pages OCR'd: {stats['ocr_pages']} / {total_pages} total "
          f"({total_pages - stats['ocr_pages']} pages needed zero OCR)")


if __name__ == "__main__":
    main()
