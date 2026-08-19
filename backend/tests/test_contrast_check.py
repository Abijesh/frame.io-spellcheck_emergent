"""Self-check for ocr_service.check_contrast, the WCAG contrast heuristic.

Pure-logic test against synthetic images: no ffmpeg, no OCR engine, no
network. Run directly:
    python backend/tests/test_contrast_check.py
(also pytest-discoverable, since the functions are named test_*).
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.ocr_service import check_contrast  # noqa: E402


def _save_box_image(bg_rgb, fg_rgb, size=(120, 40)):
    """A background-colored image with a thin foreground-colored stripe
    through the middle, standing in for anti-aliased text ink on a
    background -- enough to drive the 5th/95th percentile split."""
    w, h = size
    arr = np.full((h, w, 3), bg_rgb, dtype=np.uint8)
    arr[h // 2 - 2 : h // 2 + 2, :] = fg_rgb
    path = tempfile.mktemp(suffix=".png")
    Image.fromarray(arr).save(path)
    return path


def test_flags_low_contrast_light_gray_on_white():
    path = _save_box_image(bg_rgb=(255, 255, 255), fg_rgb=(230, 230, 230))
    hits = check_contrast(path, [{"text": "hi", "bbox": (0, 0, 120, 40)}])
    os.remove(path)
    assert len(hits) == 1
    assert hits[0]["ratio"] < hits[0]["threshold"]


def test_passes_high_contrast_black_on_white():
    path = _save_box_image(bg_rgb=(255, 255, 255), fg_rgb=(0, 0, 0))
    hits = check_contrast(path, [{"text": "hi", "bbox": (0, 0, 120, 40)}])
    os.remove(path)
    assert hits == []


def test_large_text_gets_the_lower_3to1_threshold():
    # A ratio that fails the 4.5:1 normal-text bar but clears 3:1 -- flagged
    # as small text (box height 20px < LARGE_TEXT_PX), not flagged as large.
    path = _save_box_image(bg_rgb=(255, 255, 255), fg_rgb=(120, 120, 120))
    small_hits = check_contrast(path, [{"text": "hi", "bbox": (0, 0, 120, 20)}])
    large_hits = check_contrast(path, [{"text": "hi", "bbox": (0, 0, 120, 24)}])
    os.remove(path)
    assert len(small_hits) == 1
    assert large_hits == []


def test_empty_ocr_results_returns_empty():
    assert check_contrast("does-not-matter.png", []) == []


if __name__ == "__main__":
    test_flags_low_contrast_light_gray_on_white()
    test_passes_high_contrast_black_on_white()
    test_large_text_gets_the_lower_3to1_threshold()
    test_empty_ocr_results_returns_empty()
    print("OK")
