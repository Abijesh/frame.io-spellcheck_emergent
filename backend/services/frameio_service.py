"""Frame.io helper that supports:
  1. Frame.io V2 API for owned assets via the user's legacy `fio-u-` token
     (asset detail + comment post).
  2. **Public share links** (`f.io/*` and `next.frame.io/share/...`):
     the V2/V4 public APIs do NOT expose share contents, so we load the share
     page in a headless browser and capture the `<video>` element's `currentSrc`
     — a publicly accessible signed CDN URL.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FRAMEIO_API = "https://api.frame.io/v2"
ASSET_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
SHARE_HOST_RE = re.compile(r"(f\.io/|next\.frame\.io/share/)", re.IGNORECASE)


def get_token() -> Optional[str]:
    return os.environ.get("FRAMEIO_TOKEN")


def _headers() -> dict:
    token = get_token()
    h: dict = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
        # V4-migrated accounts require this for legacy token compatibility
        h["x-frameio-legacy-token-auth"] = "true"
    return h


def is_share_link(url: str) -> bool:
    return bool(url and SHARE_HOST_RE.search(url))


def extract_asset_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = ASSET_ID_RE.search(url)
    return m.group(0) if m else None


async def follow_share_link(url: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(url)
            final_url = str(resp.url)
            return extract_asset_id(final_url) or extract_asset_id(resp.text or "")
    except Exception as exc:
        logger.warning("Failed to follow share link %s: %s", url, exc)
        return None


async def resolve_asset_id(url: str) -> Optional[str]:
    """Best-effort: pull a UUID out of the URL. For share links the LAST UUID
    (the file id) is preferred over the share id."""
    if not url:
        return None
    ids = ASSET_ID_RE.findall(url)
    if ids:
        return ids[-1]
    # follow redirects on short links like f.io/xyz
    return await follow_share_link(url)


# ---------------- public share resolver (headless browser) -----------------
async def resolve_share_video(url: str, wait_ms: int = 12000) -> Optional[dict]:
    """Load a Frame.io public share page and return {video_url, share_id, file_id}.

    Frame.io shares are JS-rendered SPAs that fetch a signed CDN URL via XHR.
    Easiest reliable extraction is to let the browser do it.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed")
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(user_agent="Mozilla/5.0")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            video_src: Optional[str] = None
            try:
                await page.wait_for_selector("video", timeout=wait_ms)
                video_src = await page.eval_on_selector(
                    "video", "v => v.currentSrc || v.src"
                )
            except Exception as exc:
                logger.warning("No <video> on share page: %s", exc)

            final_url = page.url
            await browser.close()

        if not video_src:
            return None

        ids = ASSET_ID_RE.findall(final_url)
        share_id = ids[0] if ids else None
        file_id = ids[-1] if len(ids) > 1 else (ids[0] if ids else None)
        # If file_id not in URL, try to extract from the video src
        if file_id is None or share_id == file_id:
            src_ids = ASSET_ID_RE.findall(video_src)
            if src_ids:
                file_id = src_ids[0]

        return {"video_url": video_src, "share_id": share_id, "file_id": file_id}
    except Exception as exc:
        logger.exception("resolve_share_video error: %s", exc)
        return None


# ---------------- V2 API: owned assets ----------------
async def get_asset(asset_id: str) -> Optional[dict]:
    if not get_token():
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{FRAMEIO_API}/assets/{asset_id}", headers=_headers()
            )
            if r.status_code == 200:
                return r.json()
            logger.warning(
                "Frame.io get_asset %s -> %s %s",
                asset_id,
                r.status_code,
                r.text[:200],
            )
            return None
    except Exception as exc:
        logger.exception("Frame.io get_asset error: %s", exc)
        return None


def get_video_download_url(asset: dict) -> Optional[str]:
    if not asset:
        return None
    for key in ("original", "h264_540", "h264_720", "h264_1080"):
        val = asset.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    downloads = asset.get("downloads") or {}
    if isinstance(downloads, dict):
        for v in downloads.values():
            if isinstance(v, str) and v.startswith("http"):
                return v
    return None


async def post_comment(
    asset_id: str, text: str, timestamp_seconds: Optional[float] = None
) -> dict:
    if not get_token():
        return {"ok": False, "error": "FRAMEIO_TOKEN not configured"}
    headers = {**_headers(), "Content-Type": "application/json"}
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
            return {"ok": False, "error": f"{r.status_code}: {r.text[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def download_video(url: str, dest_path: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
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
