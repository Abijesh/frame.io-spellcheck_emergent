"""Frame.io V2 (legacy) API helper.

The token format `fio-u-...` is a legacy developer token. We use the v2 API
which accepts Bearer auth directly and supports retrieving an asset and
posting comments with timestamps in seconds.
"""
from __future__ import annotations

import os
import re
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FRAMEIO_API = "https://api.frame.io/v2"
ASSET_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def get_token() -> Optional[str]:
    return os.environ.get("FRAMEIO_TOKEN")


def extract_asset_id(url: str) -> Optional[str]:
    """Try to pull a UUID asset id out of common Frame.io URL shapes.

    Supported patterns:
      https://app.frame.io/player/<asset_id>
      https://app.frame.io/reviews/<review_id>/<asset_id>
      https://app.frame.io/presentations/<id>
      Any URL containing a UUID.
    """
    if not url:
        return None
    m = ASSET_ID_RE.search(url)
    return m.group(0) if m else None


async def follow_share_link(url: str) -> Optional[str]:
    """Follow redirects (f.io short links) and extract asset id from final URL."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(url)
            final_url = str(resp.url)
            asset_id = extract_asset_id(final_url)
            if asset_id:
                return asset_id
            # Sometimes the asset id is embedded in the response body
            return extract_asset_id(resp.text or "")
    except Exception as exc:
        logger.warning("Failed to follow share link %s: %s", url, exc)
        return None


async def resolve_asset_id(url: str) -> Optional[str]:
    asset_id = extract_asset_id(url)
    if asset_id:
        return asset_id
    return await follow_share_link(url)


async def get_asset(asset_id: str) -> Optional[dict]:
    token = get_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{FRAMEIO_API}/assets/{asset_id}", headers=headers)
            if r.status_code == 200:
                return r.json()
            logger.warning("Frame.io get_asset %s -> %s %s", asset_id, r.status_code, r.text[:200])
            return None
    except Exception as exc:
        logger.exception("Frame.io get_asset error: %s", exc)
        return None


def get_video_download_url(asset: dict) -> Optional[str]:
    """Pick a downloadable URL out of an asset response."""
    if not asset:
        return None
    # `original` is usually a direct signed URL on v2
    for key in ("original", "h264_540", "h264_720", "h264_1080"):
        val = asset.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    # Sometimes nested under downloads
    downloads = asset.get("downloads") or {}
    if isinstance(downloads, dict):
        for v in downloads.values():
            if isinstance(v, str) and v.startswith("http"):
                return v
    return None


async def post_comment(
    asset_id: str, text: str, timestamp_seconds: Optional[float] = None
) -> dict:
    """Post a public comment on a Frame.io asset.

    Returns dict with `ok` and either `comment` or `error`.
    """
    token = get_token()
    if not token:
        return {"ok": False, "error": "FRAMEIO_TOKEN not configured"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload: dict = {"text": text}
    if timestamp_seconds is not None:
        payload["timestamp"] = float(timestamp_seconds)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{FRAMEIO_API}/assets/{asset_id}/comments",
                headers=headers,
                json=payload,
            )
            if r.status_code in (200, 201):
                return {"ok": True, "comment": r.json()}
            return {
                "ok": False,
                "error": f"{r.status_code}: {r.text[:300]}",
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def download_video(url: str, dest_path: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                if r.status_code != 200:
                    logger.warning("Video download failed: %s", r.status_code)
                    return False
                with open(dest_path, "wb") as f:
                    async for chunk in r.aiter_bytes(1024 * 1024):
                        f.write(chunk)
        return True
    except Exception as exc:
        logger.exception("download_video error: %s", exc)
        return False
