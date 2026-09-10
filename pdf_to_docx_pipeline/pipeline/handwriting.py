"""
Stage 3: printed vs. handwritten classification - the hardest honest problem
in this whole pipeline.

Being upfront about the limitation: there is no reliable *non-ML* way to
transcribe handwriting. Every accurate handwriting-recognition engine (Kraken,
Calamari, PyLaia, TrOCR, commercial cloud OCR) is a trained neural network,
because handwriting doesn't decompose into a small fixed set of glyph shapes
the way print does - every writer's strokes are different. Claiming otherwise
would be selling you a pipeline that quietly mangles the handwritten portions
of the document.

What *is* achievable without any learned model is deciding whether a region
LOOKS handwritten in the first place, using classical stroke geometry:
printed characters have fairly uniform stroke width and sit on a strict
baseline; handwriting doesn't. That's genuinely just image processing - no
model, no training data, no inference cost - and it's what this module does.

Given that split, the pipeline defaults to the option that guarantees
correctness at zero compute cost: a region flagged as handwriting is kept as
an embedded image in the .docx, not run through a recognizer that would
likely get it wrong. If you want an attempt at actual transcription later,
plug in Kraken (see README) as an opt-in, clearly-labeled second pass -
it's a small CPU-friendly model, not a cloud LLM, but pretrained models are
tuned mostly for historical/Latin scripts, so treat its output as a draft to
proofread, not a final answer.
"""
import cv2
import numpy as np


def stroke_irregularity_score(binary_page: np.ndarray, bbox: tuple) -> float:
    """Distance-transform-based stroke width variance within a line's
    bounding box. Higher = more irregular stroke widths = more
    handwriting-like. This is a classical (pre-deep-learning) technique;
    no neural network is used."""
    left, top, right, bottom = (int(v) for v in bbox)
    crop = binary_page[max(top, 0):bottom, max(left, 0):right]
    if crop.size == 0:
        return 0.0

    ink = cv2.bitwise_not(crop)  # ink = white after inversion (text was black on white)
    if ink.sum() == 0:
        return 0.0

    dist = cv2.distanceTransform(ink, cv2.DIST_L2, 5)
    stroke_widths = dist[dist > 0]
    if stroke_widths.size < 20:
        return 0.0

    mean_w = stroke_widths.mean()
    std_w = stroke_widths.std()
    if mean_w == 0:
        return 0.0
    return float(std_w / mean_w)  # coefficient of variation


def baseline_irregularity_score(binary_page: np.ndarray, bbox: tuple) -> float:
    """Measures how much individual character blobs wander off a straight
    baseline, normalized by their own size. Printed text sits on a rigid
    baseline; handwriting - and even careful printing by hand - drifts.
    Classical connected-component analysis, no learned model."""
    left, top, right, bottom = (int(v) for v in bbox)
    crop = binary_page[max(top, 0):bottom, max(left, 0):right]
    if crop.size == 0:
        return 0.0

    ink = cv2.bitwise_not(crop)
    n_labels, _, stats, centroids = cv2.connectedComponentsWithStats(ink, connectivity=8)
    if n_labels <= 2:  # background + at most one blob - nothing to compare
        return 0.0

    heights, cy = [], []
    for i in range(1, n_labels):  # skip background label 0
        area = stats[i, cv2.CC_STAT_AREA]
        if area < 3:
            continue  # ignore speckle noise
        heights.append(stats[i, cv2.CC_STAT_HEIGHT])
        cy.append(centroids[i][1])

    if len(cy) < 3:
        return 0.0

    mean_h = float(np.mean(heights))
    if mean_h == 0:
        return 0.0
    return float(np.std(cy) / mean_h)


def classify_embedded_image(image_bytes: bytes, threshold: float = 0.85) -> str:
    """For images embedded directly in an already-digital page (e.g. a photo
    of a printed page, or a photographed handwritten sticky note, pasted
    into an otherwise typed document). Returns 'picture', 'handwritten', or
    'text_image' (a flat scan of printed text that ended up as a picture
    rather than a text block - safe to just keep as a picture, same as a
    photo, since we're not attempting to OCR it here).

    Three cheap, classical signals, no model:
      1. Colour variance - real photographs have broad, continuous colour
         variation; scans of ink-on-paper are close to monochrome.
      2. For near-monochrome images, stroke-width irregularity.
      3. Baseline irregularity - how much characters wander off a straight
         line, which catches jittery/handwritten text even when individual
         stroke widths look fairly uniform.
    """
    if not image_bytes:
        return "picture"
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        return "picture"

    b, g, r = cv2.split(img.astype(np.int16))
    color_std = float(np.mean([np.std(r - g), np.std(g - b), np.std(r - b)]))

    if color_std > 8.0:
        return "picture"  # has real colour variation - treat as a photo

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    h, w = binary.shape
    bbox = (0, 0, w, h)
    stroke_score = stroke_irregularity_score(binary, bbox)
    baseline_score = baseline_irregularity_score(binary, bbox)

    if stroke_score >= threshold or baseline_score >= 0.28:
        return "handwritten"
    return "text_image"


def classify_line(binary_page: np.ndarray, bbox: tuple, confidence: float,
                   threshold: float = 0.85) -> str:
    """Returns 'handwritten' or 'printed'.

    Two signals, both classical (no ML):
      1. Stroke-width coefficient of variation above `threshold` - printed
         fonts are typically well under this; handwriting is typically
         well over it.
      2. Tesseract's own recognition confidence as a secondary signal -
         Tesseract's printed-text-trained model tends to score handwriting
         low even when it doesn't crash, so a low-confidence line paired
         with high stroke irregularity is a strong combined signal.

    This is a heuristic, not a certainty - tune `threshold` against a few
    pages of your own real documents before trusting it unattended. Scripts
    that stack diacritics/tone marks above or below the main body of a
    letter (Thai, Vietnamese, Arabic, Devanagari...) naturally show more
    "baseline" spread than Latin print, so geometry alone will over-trigger
    on those scripts - which is why confidence is a required second vote,
    not just a tiebreaker: printed text (in any script) that Tesseract
    reads with high confidence is kept as printed even if the geometry
    looks unusual, and only genuinely low-confidence + irregular lines get
    flagged. Safer failure mode either way: an over-eager "handwriting"
    flag just means a perfectly good printed line gets kept as an image
    instead of selectable text, which is a minor cosmetic loss - the
    reverse mistake risks silently mangled text.
    """
    irregularity = stroke_irregularity_score(binary_page, bbox)
    baseline = baseline_irregularity_score(binary_page, bbox)
    if confidence < 80 and (irregularity >= threshold or baseline >= 0.28):
        return "handwritten"
    return "printed"
