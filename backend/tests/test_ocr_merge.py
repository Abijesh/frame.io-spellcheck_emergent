"""Self-check for ocr_service.merge_instances, the text-instance dedup logic.

Pure-logic test: no ffmpeg, no OCR engine, no network. Run directly:
    python backend/tests/test_ocr_merge.py
(also pytest-discoverable, since the functions are named test_*).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.ocr_service import merge_instances  # noqa: E402


def _hit(text, conf=0.9, box=(0, 0, 100, 20)):
    return {"text": text, "bbox": box, "conf": conf}


def test_merges_consecutive_matching_frames_into_one_instance():
    frames = [
        (0.0, [_hit("Wellcome", conf=0.7)]),
        (0.5, [_hit("Wellcome", conf=0.95)]),  # clearest -> representative frame
        (1.0, [_hit("Wellcome", conf=0.8)]),
    ]
    instances = merge_instances(frames)
    assert len(instances) == 1
    inst = instances[0]
    assert inst["start"] == 0.0
    assert inst["end"] == 1.0
    assert inst["frame_index"] == 1


def test_dissimilar_text_starts_a_new_instance():
    frames = [
        (0.0, [_hit("Chapter One")]),
        (0.5, [_hit("Chapter One")]),
        (1.0, [_hit("Completely Different Title")]),
    ]
    instances = merge_instances(frames)
    assert len(instances) == 2
    assert instances[0]["end"] == 0.5
    assert instances[1]["start"] == 1.0


def test_empty_frame_closes_the_open_instance():
    frames = [
        (0.0, [_hit("Lower Third")]),
        (0.5, []),  # text disappears
        (1.0, [_hit("Lower Third")]),  # reappears -> a second, distinct instance
    ]
    instances = merge_instances(frames)
    assert len(instances) == 2
    assert instances[0]["end"] == 0.0
    assert instances[1]["start"] == 1.0


def test_no_text_anywhere_yields_no_instances():
    assert merge_instances([(0.0, []), (0.5, []), (1.0, [])]) == []


if __name__ == "__main__":
    test_merges_consecutive_matching_frames_into_one_instance()
    test_dissimilar_text_starts_a_new_instance()
    test_empty_frame_closes_the_open_instance()
    test_no_text_anywhere_yields_no_instances()
    print("ocr_service.merge_instances: all checks passed")
