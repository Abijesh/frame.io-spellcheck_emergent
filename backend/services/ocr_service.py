"""Local OCR pass: finds which densely-sampled frames contain text, then
groups consecutive frames with matching text into single instances so
Gemini is only called once per distinct piece of on-screen text (its
clearest frame) instead of once per sample.

`easyocr` is imported lazily inside `ocr_frame` so `merge_instances` and
`crop_thumbnail` stay importable/testable without the (heavy) OCR
dependency installed.
"""
from __future__ import annotations

import difflib
import io
import logging
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.6  # consecutive frames counted as "same text"
STABILITY_SHIFT_THRESHOLD = 0.08  # fraction of text-box size counted as "settled"
STABLE_TEXT_RATIO = 0.95  # consecutive frames counted as textually unchanged
MIN_CHARS_PER_HIT = 2  # isolated single characters are near-never a real caption
# A reading that only ever showed up in one sample, with nothing to corroborate
# it, needs to be this confident to survive -- otherwise it's more likely a
# misread of a blurry/textured background than genuine on-screen text.
MIN_CONFIDENCE_FOR_LONE_SAMPLE = 0.65

# WCAG 2.x contrast minimums: 4.5:1 for normal text, 3:1 for "large" text
# (>=~24px / bold >=~19px, using box height as a proxy for point size here).
CONTRAST_AA_NORMAL = 4.5
CONTRAST_AA_LARGE = 3.0
LARGE_TEXT_PX = 24

_reader = None
_reader_lock = threading.Lock()


def _get_reader():
    global _reader
    # Guards against two concurrent first-callers each constructing their own
    # Reader (each load is expensive). Cheap insurance even though the
    # current pipeline calls ocr_frame sequentially.
    if _reader is None:
        with _reader_lock:
            if _reader is None:
                import easyocr

                _reader = easyocr.Reader(["en"], gpu=True)
    return _reader


def ocr_frame(image_path: str) -> List[dict]:
    """Read all text boxes in a frame: [{"text","bbox":(x0,y0,x1,y1),"conf"}]."""
    reader = _get_reader()
    try:
        results = reader.readtext(image_path)
    except Exception as exc:
        logger.warning("EasyOCR failed on %s: %s", image_path, exc)
        return []

    out: List[dict] = []
    for bbox, text, conf in results:
        if len(text.strip()) < MIN_CHARS_PER_HIT:
            # An isolated single character (a stray digit, a UI glyph, film
            # grain misread as a letter) isn't meaningful on-screen text a
            # viewer would actually read -- drop it before it ever becomes an
            # "instance" worth a Gemini call or a contrast check.
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        out.append(
            {
                "text": text,
                "bbox": (min(xs), min(ys), max(xs), max(ys)),
                "conf": float(conf),
            }
        )
    return out


def _signature(frame_results: List[dict]) -> str:
    return " ".join(r["text"] for r in frame_results).strip().lower()


def _bbox_union(ocr_results: List[dict]) -> Optional[Tuple[float, float, float, float]]:
    if not ocr_results:
        return None
    xs0 = [r["bbox"][0] for r in ocr_results]
    ys0 = [r["bbox"][1] for r in ocr_results]
    xs1 = [r["bbox"][2] for r in ocr_results]
    ys1 = [r["bbox"][3] for r in ocr_results]
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _bbox_shift(a: Optional[Tuple], b: Optional[Tuple]) -> float:
    """How much a text box moved/resized between two frames, as a fraction of
    its own size: ~0 means visually settled (same place, same size as the
    other frame), large means it's still animating in/out (sliding, scaling,
    a wipe mask moving across it, etc)."""
    if a is None or b is None:
        return float("inf")
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    size = max(ax1 - ax0, ay1 - ay0, 1.0)
    return (abs(ax0 - bx0) + abs(ay0 - by0) + abs(ax1 - bx1) + abs(ay1 - by1)) / size


def merge_instances(
    frames: List[Tuple[float, List[dict]]],
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> List[dict]:
    """Group consecutive frames with matching on-screen text into instances.

    `frames` is [(timestamp, ocr_results)] in time order, where each
    ocr_results is what `ocr_frame` returns for that frame.

    Returns one dict per distinct piece of on-screen text:
    {"start": float, "end": float, "frame_index": int, "ocr_results": [...]}
    — frame_index/ocr_results point at the representative frame used as both
    Gemini's input and the contrast check's input: preferentially a frame
    that's both visually settled (its text box barely moved/resized versus a
    neighboring sample -- not mid slide/scale animation) *and* textually
    unchanged from that neighbor (rules out a fixed-position reveal, e.g. a
    typewriter effect, where the box never moves but the text is still
    incomplete). Falls back to pure highest-confidence if no sample ever
    looks settled (e.g. only one sample landed in the instance's span, or the
    text never stops moving -- a fast ticker).

    Instances that only ever showed up in a single sample, and weren't read
    confidently even then, are dropped entirely (see MIN_CONFIDENCE_FOR_LONE_
    SAMPLE) -- real on-screen text tends to hold for multiple samples at any
    reasonable sampling interval, so an uncorroborated one-off reading is
    more likely a misread of a blurry/textured background.

    Ponytail: similarity is judged on the whole frame's concatenated text
    (via difflib against the *previous matched* frame, not the instance's
    first frame, so gradually-appearing/typewriter text still merges into one
    instance). Known ceilings: (1) two unrelated text blocks that both change
    between samples read as "different" and split into separate instances
    even if unrelated text coexists on screen; (2) comparing against the
    previous frame means slow drift can in principle walk an instance from
    one string to an unrelated one over many frames without ever dropping
    below threshold. Upgrade path: per-text-region bounding-box IoU tracking
    instead of whole-frame string similarity.
    """
    instances: List[dict] = []
    current: Optional[dict] = None
    current_sig = ""

    for idx, (ts, results) in enumerate(frames):
        sig = _signature(results)
        if not sig:
            current = None
            current_sig = ""
            continue

        if current is not None:
            ratio = difflib.SequenceMatcher(None, sig, current_sig).ratio()
            if ratio >= similarity_threshold:
                current["end"] = ts
                current["frames"].append(idx)
                current_sig = sig
                continue

        current = {"start": ts, "end": ts, "frames": [idx]}
        instances.append(current)
        current_sig = sig

    for inst in instances:
        frame_idxs = inst["frames"]
        boxes = [_bbox_union(frames[i][1]) for i in frame_idxs]
        sigs = [_signature(frames[i][1]) for i in frame_idxs]
        stable_positions = set()
        for k in range(1, len(frame_idxs)):
            shift = _bbox_shift(boxes[k - 1], boxes[k])
            text_ratio = difflib.SequenceMatcher(None, sigs[k - 1], sigs[k]).ratio()
            if shift <= STABILITY_SHIFT_THRESHOLD and text_ratio >= STABLE_TEXT_RATIO:
                stable_positions.add(k - 1)
                stable_positions.add(k)
        candidates = [frame_idxs[p] for p in stable_positions] or frame_idxs

        best_idx = max(
            candidates, key=lambda i: sum(r["conf"] for r in frames[i][1])
        )
        inst["frame_index"] = best_idx
        inst["ocr_results"] = frames[best_idx][1]
        del inst["frames"]

    def _worth_keeping(inst: dict) -> bool:
        if inst["start"] != inst["end"]:
            return True  # corroborated by 2+ samples
        results = inst["ocr_results"]
        avg_conf = sum(r["conf"] for r in results) / max(len(results), 1)
        return avg_conf >= MIN_CONFIDENCE_FOR_LONE_SAMPLE

    return [inst for inst in instances if _worth_keeping(inst)]


def _relative_luminance(rgb):
    """WCAG relative luminance for an array of sRGB pixels, shape (..., 3), 0-255."""
    c = rgb / 255.0
    c = (c <= 0.03928) * (c / 12.92) + (c > 0.03928) * (((c + 0.055) / 1.055) ** 2.4)
    return c[..., 0] * 0.2126 + c[..., 1] * 0.7152 + c[..., 2] * 0.0722


def check_contrast(image_path: str, ocr_results: List[dict]) -> List[dict]:
    """WCAG-style contrast check for each detected text box on one frame.
    Returns the boxes that fall below the applicable AA threshold:
    [{"text", "bbox", "ratio", "threshold"}].

    Ponytail: approximates foreground/background color via the 5th/95th
    luminance percentile within each box rather than a real text-ink mask --
    reasonable when text sits on a fairly uniform patch, prone to false
    positives on outlined/drop-shadowed captions (deliberately legible
    despite "bad" raw contrast) or a highly textured background. Also checks
    only the instance's single representative frame, not its full on-screen
    duration, so a brief bad-contrast moment within a longer instance can be
    missed. Upgrade path: a real glyph mask (e.g. from the OCR detector's own
    confidence map) and multi-frame sampling across the instance's span.
    """
    if not ocr_results:
        return []
    import numpy as np
    from PIL import Image

    out: List[dict] = []
    with Image.open(image_path) as img:
        arr = np.asarray(img.convert("RGB"))
        h, w = arr.shape[:2]
        for r in ocr_results:
            x0, y0, x1, y1 = r["bbox"]
            x0, y0 = max(0, int(x0)), max(0, int(y0))
            x1, y1 = min(w, int(x1)), min(h, int(y1))
            if x1 <= x0 or y1 <= y0:
                continue
            box = arr[y0:y1, x0:x1].astype(np.float64)
            lum = _relative_luminance(box)
            l_dark, l_light = np.percentile(lum, [5, 95])
            ratio = (l_light + 0.05) / (l_dark + 0.05)
            threshold = (
                CONTRAST_AA_LARGE if (y1 - y0) >= LARGE_TEXT_PX else CONTRAST_AA_NORMAL
            )
            if ratio < threshold:
                out.append(
                    {
                        "text": r["text"],
                        "bbox": r["bbox"],
                        "ratio": round(float(ratio), 2),
                        "threshold": threshold,
                    }
                )
    return out


def crop_thumbnail(
    image_path: str, ocr_results: List[dict], pad: int = 12
) -> Optional[bytes]:
    """Crop the union of detected text boxes (padded) from a frame as JPEG bytes."""
    if not ocr_results:
        return None
    from PIL import Image

    xs0 = [r["bbox"][0] for r in ocr_results]
    ys0 = [r["bbox"][1] for r in ocr_results]
    xs1 = [r["bbox"][2] for r in ocr_results]
    ys1 = [r["bbox"][3] for r in ocr_results]

    with Image.open(image_path) as img:
        w, h = img.size
        box = (
            max(0, int(min(xs0)) - pad),
            max(0, int(min(ys0)) - pad),
            min(w, int(max(xs1)) + pad),
            min(h, int(max(ys1)) + pad),
        )
        crop = img.crop(box).convert("RGB")
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
