"""Video frame extraction using ffmpeg."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import List, Tuple

logger = logging.getLogger(__name__)


def get_duration(video_path: str) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            timeout=30,
        )
        return float(out.decode().strip())
    except Exception as exc:
        logger.warning("ffprobe duration failed: %s", exc)
        return 0.0


async def extract_frames(
    video_path: str, out_dir: str, interval_seconds: float = 2.0
) -> List[Tuple[float, str]]:
    """Extract one frame every `interval_seconds`. Returns list of (timestamp, path)."""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "frame_%05d.jpg")
    fps = 1.0 / max(interval_seconds, 0.1)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-vf",
        f"fps={fps},scale=1280:-2",
        "-q:v",
        "3",
        pattern,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    frames: List[Tuple[float, str]] = []
    i = 1
    while True:
        p = os.path.join(out_dir, f"frame_{i:05d}.jpg")
        if not os.path.exists(p):
            break
        ts = (i - 1) * interval_seconds
        frames.append((ts, p))
        i += 1
    return frames
