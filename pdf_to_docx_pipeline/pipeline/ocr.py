"""
Stage 2: OCR for pages that have no text layer.

Everything here runs locally on CPU:
  - OpenCV for deskew / denoise / binarization (classical image processing,
    no neural network, effectively free in power terms)
  - Tesseract 5 for the actual character recognition. Tesseract's recognition
    step does use a small LSTM network internally, but it is orders of
    magnitude smaller than a vision-LLM call: it runs on CPU in well under a
    second per page and needs no GPU, no network round-trip, and no
    per-token billing. That's the trade we're making deliberately: a tiny
    local model instead of a large remote one.

Multi-language handling: Tesseract does not auto-detect which language a
region is in - you tell it which language pack(s) to try, and it does
combined recognition across all of them for a page (see DEFAULT_LANGS).
For a page that's overwhelmingly one script this works well out of the box.
For pages that genuinely interleave unrelated scripts in different regions,
the practical fix is running Tesseract's own layout segmentation once, then
re-checking each detected line with `langdetect` on the recognized text and
flagging low-confidence lines for a second, targeted OCR pass - see
`retag_language()` below.
"""
import cv2
import numpy as np
import pytesseract
from PIL import Image
import io
import re
from langdetect import detect, DetectorFactory, LangDetectException

DetectorFactory.seed = 0  # deterministic langdetect results

DEFAULT_LANGS = "eng+tha+chi_sim"  # extend as needed: apt install tesseract-ocr-<code>

# Tesseract 3-letter codes we know how to map back from langdetect's
# 2-letter ISO codes, for the optional re-tagging pass.
LANGDETECT_TO_TESSERACT = {
    "en": "eng", "th": "tha", "zh-cn": "chi_sim", "zh-tw": "chi_tra",
    "ja": "jpn", "ko": "kor", "fr": "fra", "de": "deu", "es": "spa",
}


def _estimate_skew_angle(gray: np.ndarray) -> float:
    inverted = cv2.bitwise_not(gray)
    coords = np.column_stack(np.where(inverted > 30))
    if coords.shape[0] < 50:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    return 0.0 if abs(angle) < 0.05 else angle


def _rotate(img: np.ndarray, angle: float) -> np.ndarray:
    if angle == 0.0:
        return img
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    border = cv2.BORDER_REPLICATE
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=border)


def _decode(png_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def preprocess(deskewed_gray: np.ndarray) -> np.ndarray:
    """Denoise -> Otsu binarize. Deskewing happens once, upstream, so it can
    be shared between region segmentation and OCR."""
    denoised = cv2.medianBlur(deskewed_gray, 3)
    _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


_THAI_RANGE = "ก-๛"
_THAI_GAP_RE = re.compile(f"(?<=[{_THAI_RANGE}]) (?=[{_THAI_RANGE}])")


def normalize_thai_spacing(text: str) -> str:
    """Tesseract's Thai model frequently emits a space between every glyph,
    because (unlike Latin scripts) Thai doesn't separate words with spaces
    at all - Tesseract's per-character segmentation gets mistaken for
    per-word segmentation downstream. This collapses single spaces that sit
    strictly between two Thai characters, which fixes the common
    "ร า ย ชื ่ อ" -> "รายชื่อ" case without touching genuine spacing
    elsewhere (e.g. between a Thai clause and a following English word/number).
    This is plain regex/string handling, not a language model."""
    prev = None
    cleaned = text
    while cleaned != prev:  # spaces can be adjacent to more than one gap
        prev = cleaned
        cleaned = _THAI_GAP_RE.sub("", cleaned)
    return cleaned


def segment_regions(color_img: np.ndarray) -> tuple[list[tuple], list[tuple]]:
    """Splits a rasterized page into candidate text blocks and picture
    blocks BEFORE running OCR, so photos/portraits/graphics never get
    force-fed through Tesseract (which produces garbage text and wastes
    the one part of this pipeline that does real compute). Classical CV
    only: threshold + morphological merge + connected-component shape
    analysis, no model.

    Returns (text_bboxes, picture_bboxes), each a list of (left, top, right, bottom).
    """
    gray = cv2.cvtColor(color_img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = cv2.bitwise_not(binary)  # ink = white

    # Calibrate the merge kernel to this page's own character size instead of
    # a fixed pixel count - a fixed kernel silently breaks at any scan
    # resolution other than the one it was tuned on, which is exactly the
    # kind of bug you don't notice until a higher-DPI scan quietly produces
    # garbage. Estimate typical glyph height from raw (pre-merge) connected
    # components, then size the kernel off that.
    n_raw, _, raw_stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    raw_heights = [raw_stats[i, cv2.CC_STAT_HEIGHT] for i in range(1, n_raw)
                   if 2 <= raw_stats[i, cv2.CC_STAT_AREA] < ink.size * 0.01]
    glyph_h = int(np.median(raw_heights)) if raw_heights else 12
    glyph_h = max(glyph_h, 6)

    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (glyph_h * 6, int(glyph_h * 1.4)))
    merged = cv2.dilate(ink, kernel, iterations=2)
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    page_area = color_img.shape[0] * color_img.shape[1]
    text_bboxes, picture_bboxes = [], []

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        if area < page_area * 0.002:
            continue  # speckle/noise, not a real block

        color_crop = color_img[y:y + h, x:x + w]
        b, g, r = cv2.split(color_crop.astype(np.int16))
        color_std = float(np.mean([np.std(r - g), np.std(g - b), np.std(r - b)]))

        ink_crop = ink[y:y + h, x:x + w]
        ink_ratio = float(np.count_nonzero(ink_crop)) / area

        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(ink_crop, connectivity=8)
        component_areas = [stats[i, cv2.CC_STAT_AREA] for i in range(1, n_labels)
                            if stats[i, cv2.CC_STAT_AREA] >= 3]
        largest_component_frac = (max(component_areas) / area) if component_areas else 0.0
        n_components = len(component_areas)

        # Text blocks: many small, similarly-sized ink components (character
        # strokes), low colour variance, moderate ink coverage.
        # Picture blocks: real colour variation OR one/few large blobs
        # (a photo's edges/silhouette merge into a handful of big regions
        # rather than dozens of small character-sized ones).
        looks_photographic = color_std > 8.0
        looks_like_a_graphic = (largest_component_frac > 0.35) and (n_components < 8)

        if looks_photographic or looks_like_a_graphic:
            picture_bboxes.append((x, y, x + w, y + h))
        else:
            text_bboxes.append((x, y, x + w, y + h))

    return text_bboxes, picture_bboxes


def process_scanned_page(png_bytes: bytes, langs: str = DEFAULT_LANGS) -> dict:
    """Full stage-2 pipeline for one image-only page:
      decode -> deskew once -> segment into text/picture regions ->
      mask out picture regions -> OCR only the text regions.

    This is the fix for the failure mode where a photo/portrait on a
    scanned page gets forced through Tesseract and comes back as garbage
    text: we now decide "is this actually text?" with pure image-geometry
    analysis (segment_regions) before spending any OCR compute on it.

    Returns a dict: {"lines": [...], "pictures": [png_bytes, ...],
    "processed": binary_image_for_handwriting_checks}
    """
    color = _decode(png_bytes)
    gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    angle = _estimate_skew_angle(gray)
    color = _rotate(color, angle)
    gray = _rotate(gray, angle)
    binary = preprocess(gray)

    text_bboxes, picture_bboxes = segment_regions(color)

    # mask every picture region white in the binary image so Tesseract
    # never sees it
    masked = binary.copy()
    for (l, t, r, b) in picture_bboxes:
        cv2.rectangle(masked, (l, t), (r, b), 255, thickness=-1)

    picture_crops = []
    for (l, t, r, b) in picture_bboxes:
        crop = color[t:b, l:r]
        ok, buf = cv2.imencode(".png", crop)
        if ok:
            picture_crops.append(buf.tobytes())

    pil_img = Image.fromarray(masked)
    data = pytesseract.image_to_data(
        pil_img, lang=langs, output_type=pytesseract.Output.DICT,
        config="--psm 6",
    )

    lines: dict[tuple, dict] = {}
    n = len(data["text"])
    for i in range(n):
        word = data["text"][i].strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = {
                "words": [], "confs": [],
                "left": data["left"][i], "top": data["top"][i],
                "right": data["left"][i] + data["width"][i],
                "bottom": data["top"][i] + data["height"][i],
            }
        l = lines[key]
        l["words"].append(word)
        l["confs"].append(float(data["conf"][i]))
        l["left"] = min(l["left"], data["left"][i])
        l["top"] = min(l["top"], data["top"][i])
        l["right"] = max(l["right"], data["left"][i] + data["width"][i])
        l["bottom"] = max(l["bottom"], data["top"][i] + data["height"][i])

    results = []
    for l in lines.values():
        results.append({
            "text": normalize_thai_spacing(" ".join(l["words"])),
            "confidence": sum(l["confs"]) / len(l["confs"]),
            "bbox": (l["left"], l["top"], l["right"], l["bottom"]),
        })
    results.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]))  # top-to-bottom, left-to-right

    return {"lines": results, "pictures": picture_crops, "processed": masked}


def tag_language(text: str) -> str:
    """Lightweight, non-neural-net-per-call language tag using langdetect's
    statistical n-gram model (loaded once, runs in milliseconds, no GPU)."""
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"
