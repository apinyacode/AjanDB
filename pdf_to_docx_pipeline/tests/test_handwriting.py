import cv2
import numpy as np

from pipeline import handwriting


def _render_text(text, jitter=False, font_scale=1.0, thickness=2):
    img = np.full((120, 500, 3), 255, dtype=np.uint8)
    x = 10
    rng = np.random.RandomState(7)
    if not jitter:
        cv2.putText(img, text, (x, 70), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness)
    else:
        for ch in text:
            y = 70 + int(rng.randint(-8, 8))
            cv2.putText(img, ch, (x, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0),
                        max(1, thickness + rng.randint(-1, 2)))
            x += 22 + rng.randint(-2, 3)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def test_printed_text_has_low_irregularity():
    binary = _render_text("Printed Text Line")
    h, w = binary.shape
    score = handwriting.stroke_irregularity_score(binary, (0, 0, w, h))
    assert score < 0.85


def test_jittered_text_has_higher_baseline_irregularity_than_printed():
    printed = _render_text("Printed Text Line")
    jittered = _render_text("Printed Text Line", jitter=True)
    h, w = printed.shape

    printed_baseline = handwriting.baseline_irregularity_score(printed, (0, 0, w, h))
    jittered_baseline = handwriting.baseline_irregularity_score(jittered, (0, 0, w, h))

    assert jittered_baseline > printed_baseline


def test_classify_line_keeps_high_confidence_printed_text_as_printed():
    binary = _render_text("Printed Text Line")
    h, w = binary.shape
    label = handwriting.classify_line(binary, (0, 0, w, h), confidence=95)
    assert label == "printed"


def test_classify_line_flags_low_confidence_irregular_text_as_handwritten():
    jittered = _render_text("Printed Text Line", jitter=True)
    h, w = jittered.shape
    label = handwriting.classify_line(jittered, (0, 0, w, h), confidence=30, threshold=0.3)
    assert label == "handwritten"


def test_classify_embedded_image_detects_colour_photo_as_picture():
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[:, :, 0] = 40   # blue channel
    img[:, :, 1] = 180  # green channel
    img[:, :, 2] = 90   # red channel
    # add gradient so channels diverge (real colour variance)
    for i in range(100):
        img[i, :, 2] = (img[i, :, 2].astype(int) + i) % 256
    ok, buf = cv2.imencode(".png", img)
    assert ok
    assert handwriting.classify_embedded_image(buf.tobytes()) == "picture"


def test_classify_embedded_image_returns_picture_for_empty_bytes():
    assert handwriting.classify_embedded_image(b"") == "picture"
