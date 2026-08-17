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
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.6  # consecutive frames counted as "same text"

_reader = None


def _get_reader():
    global _reader
    if _reader is None:
        import easyocr

        _reader = easyocr.Reader(["en"], gpu=False)
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
        if not text.strip():
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


def merge_instances(
    frames: List[Tuple[float, List[dict]]],
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> List[dict]:
    """Group consecutive frames with matching on-screen text into instances.

    `frames` is [(timestamp, ocr_results)] in time order, where each
    ocr_results is what `ocr_frame` returns for that frame.

    Returns one dict per distinct piece of on-screen text:
    {"start": float, "end": float, "frame_index": int, "ocr_results": [...]}
    — frame_index/ocr_results point at the frame with the highest total OCR
    confidence in that instance's span, used as Gemini's input.

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
        best_idx = max(
            inst["frames"], key=lambda i: sum(r["conf"] for r in frames[i][1])
        )
        inst["frame_index"] = best_idx
        inst["ocr_results"] = frames[best_idx][1]
        del inst["frames"]

    return instances


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
