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


def test_prefers_a_visually_settled_frame_over_a_mid_animation_one():
    # Frame 0: text still sliding in (offset box) but happens to have the
    # highest raw OCR confidence -- picking by confidence alone would grab
    # this one, but it'd be mid-animation (blurry/distorted) in a real video.
    # Frames 1-2: text has settled into its final position and holds there.
    frames = [
        (0.0, [_hit("Subscribe Now", conf=0.97, box=(40, 10, 130, 28))]),
        (0.5, [_hit("Subscribe Now", conf=0.85, box=(0, 0, 100, 20))]),
        (1.0, [_hit("Subscribe Now", conf=0.80, box=(0, 0, 100, 20))]),
    ]
    instances = merge_instances(frames)
    assert len(instances) == 1
    # Settled frame (1), not the higher-confidence but still-moving frame (0).
    assert instances[0]["frame_index"] == 1


def test_prefers_complete_text_over_a_still_growing_reveal():
    # A fixed-position caption box where the text reveals progressively
    # (typewriter effect) -- bbox-only stability would be fooled, since the
    # box itself never moves even while the text is still incomplete. Only
    # the last two samples, where the full word has finished appearing and
    # holds steady, are both position- *and* text-stable.
    box = (0, 0, 200, 20)
    frames = [
        (0.0, [_hit("diffic", conf=0.90, box=box)]),
        (1.5, [_hit("difficul", conf=0.92, box=box)]),
        (3.0, [_hit("difficulties", conf=0.85, box=box)]),
        (4.5, [_hit("difficulties", conf=0.88, box=box)]),
    ]
    instances = merge_instances(frames)
    assert len(instances) == 1
    picked = frames[instances[0]["frame_index"]][1][0]["text"]
    assert picked == "difficulties"  # not the still-typing "diffic"/"difficul"


def test_drops_an_unconfident_reading_seen_only_once():
    # A single low-confidence sample with nothing to corroborate it -- the
    # profile of a misread background texture, not a real caption.
    frames = [
        (0.0, []),
        (1.5, [_hit("pErEatnt", conf=0.35)]),
        (3.0, []),
    ]
    assert merge_instances(frames) == []


def test_keeps_a_confident_reading_seen_only_once():
    # A single sample can still be trusted if the OCR was genuinely sure --
    # e.g. a caption that only overlapped one sample point by chance.
    frames = [
        (0.0, []),
        (1.5, [_hit("Chapter Two", conf=0.93)]),
        (3.0, []),
    ]
    instances = merge_instances(frames)
    assert len(instances) == 1
    assert instances[0]["start"] == instances[0]["end"] == 1.5


def test_falls_back_to_highest_confidence_when_nothing_ever_settles():
    # Every sample lands in a different place (e.g. a fast scrolling ticker)
    # -- nothing counts as "stable", so this falls back to the old rule:
    # whichever sample OCR'd most confidently.
    frames = [
        (0.0, [_hit("Breaking News", conf=0.6, box=(0, 0, 100, 20))]),
        (0.5, [_hit("Breaking News", conf=0.95, box=(200, 0, 300, 20))]),
        (1.0, [_hit("Breaking News", conf=0.7, box=(400, 0, 500, 20))]),
    ]
    instances = merge_instances(frames)
    assert instances[0]["frame_index"] == 1


if __name__ == "__main__":
    test_merges_consecutive_matching_frames_into_one_instance()
    test_dissimilar_text_starts_a_new_instance()
    test_empty_frame_closes_the_open_instance()
    test_no_text_anywhere_yields_no_instances()
    test_prefers_a_visually_settled_frame_over_a_mid_animation_one()
    test_prefers_complete_text_over_a_still_growing_reveal()
    test_drops_an_unconfident_reading_seen_only_once()
    test_keeps_a_confident_reading_seen_only_once()
    test_falls_back_to_highest_confidence_when_nothing_ever_settles()
    print("ocr_service.merge_instances: all checks passed")
