"""Frame.io public share interaction via Playwright (no Adobe OAuth needed).

Two phases per analysis:
  1. resolve_share_video(url, password) -> {video_url, file_id, password_required, error}
     Opens the share page, fills password if needed, returns the signed CDN URL
     for the <video> element so we can download for OCR.

  2. submit_guest_comments(url, password, comments) -> {posted, failed, error}
     Reopens the share page (a separate browser session), accepts the guest
     name/email dialog once with our fixed identity, then seeks the video to
     each comment's timestamp and submits the text.

Selectors (verified on next.frame.io 2026):
  password input  : input[type=password]
  password submit : button:has-text("Submit") | button[type=submit]
  video player    : video[data-testid="video-player"]
  composer        : [data-testid="create-comment-comment-composer"]
  submit comment  : [data-testid="composer-submit-button"]
  name field      : input[aria-label="Your name"]
  email field     : input[aria-label="Your email"]
  save button     : role=dialog -> button:has-text("Save")
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GUEST_NAME = "Spellchecker"
GUEST_EMAIL = "spellchecker@proof.io"
ASSET_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _launch_args():
    return ["--no-sandbox", "--disable-dev-shm-usage"]


async def _new_page(p):
    browser = await p.chromium.launch(headless=True, args=_launch_args())
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0",
        viewport={"width": 1280, "height": 800},
    )
    page = await ctx.new_page()
    return browser, page


async def _maybe_enter_password(page, password: Optional[str]) -> dict:
    """Returns {'ok': bool, 'password_required': bool, 'error': str|None}."""
    try:
        pw_input = await page.query_selector("input[type=password]")
    except Exception:
        pw_input = None
    if not pw_input:
        return {"ok": True, "password_required": False, "error": None}

    if not password:
        return {
            "ok": False,
            "password_required": True,
            "error": "This Frame.io share is password-protected.",
        }
    await pw_input.fill(password)
    # Submit by Enter (works across Frame.io UI variants)
    await page.keyboard.press("Enter")
    try:
        await page.wait_for_selector(
            "video[data-testid='video-player']", timeout=10000
        )
    except Exception:
        # Wrong password → password field still visible
        still_there = await page.query_selector("input[type=password]")
        if still_there:
            return {
                "ok": False,
                "password_required": True,
                "error": "Wrong password.",
            }
    return {"ok": True, "password_required": False, "error": None}


async def resolve_share_video(url: str, password: Optional[str] = None) -> dict:
    """Returns {video_url, file_id, password_required, error}."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "playwright not installed"}

    out: dict = {
        "video_url": None,
        "file_id": None,
        "password_required": False,
        "error": None,
    }
    async with async_playwright() as p:
        browser, page = await _new_page(p)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)

            pw_res = await _maybe_enter_password(page, password)
            if not pw_res["ok"]:
                out["password_required"] = pw_res["password_required"]
                out["error"] = pw_res["error"]
                return out

            # Give the SPA generous time to render the video element
            video_src = None
            for _ in range(20):  # up to ~30s
                try:
                    video_src = await page.eval_on_selector(
                        "video[data-testid='video-player'], video",
                        "v => v && (v.currentSrc || v.src)",
                    )
                except Exception:
                    video_src = None
                if video_src:
                    break
                await page.wait_for_timeout(1500)

            if not video_src:
                out["error"] = "Video player did not load on the share page."
                return out

            out["video_url"] = video_src
            final_url = page.url
            ids = ASSET_ID_RE.findall(final_url)
            out["file_id"] = ids[-1] if ids else None
            if not out["file_id"] and video_src:
                src_ids = ASSET_ID_RE.findall(video_src)
                out["file_id"] = src_ids[0] if src_ids else None
            return out
        except Exception as exc:
            logger.exception("resolve_share_video error: %s", exc)
            out["error"] = str(exc)
            return out
        finally:
            await browser.close()


async def _accept_guest_identity_if_prompted(page) -> bool:
    """If the 'Let others know who you are' dialog is open, fill name/email and save."""
    try:
        dialog = await page.wait_for_selector("[role=dialog]", timeout=2000)
    except Exception:
        return False
    if not dialog:
        return False
    text = (await dialog.inner_text()) or ""
    if "who you are" not in text.lower() and "Your name" not in text:
        return False
    try:
        await page.fill("input[aria-label='Your name']", GUEST_NAME)
        await page.fill("input[aria-label='Your email']", GUEST_EMAIL)
        # Save button inside the dialog
        await page.locator("[role=dialog] button:has-text('Save')").first.click()
        await page.wait_for_timeout(1200)
        return True
    except Exception as exc:
        logger.warning("guest identity dialog handling failed: %s", exc)
        return False


async def _seek(page, seconds: float) -> None:
    s = max(float(seconds), 0.0)
    await page.evaluate(
        "(t) => { const v = document.querySelector(\"video[data-testid='video-player']\") "
        "|| document.querySelector('video'); if (v) { v.currentTime = t; }}",
        s,
    )
    await page.wait_for_timeout(350)


async def _type_and_submit(page, text: str) -> bool:
    composer = page.locator("[data-testid=create-comment-comment-composer]")
    try:
        await composer.click(timeout=4000)
    except Exception:
        return False
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    await page.keyboard.type(text, delay=8)
    await page.wait_for_timeout(250)
    submit = page.locator("[data-testid=composer-submit-button]")
    try:
        await submit.click(timeout=4000)
    except Exception:
        await page.keyboard.press("Control+Enter")
    return True


async def submit_guest_comments(
    url: str,
    password: Optional[str],
    comments: list,
) -> dict:
    """`comments`: list of {timestamp_sec: float, text: str}.

    Returns {posted: int, failed: int, error: str|None, password_required: bool}.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"posted": 0, "failed": len(comments), "error": "playwright not installed"}

    result = {
        "posted": 0,
        "failed": 0,
        "error": None,
        "password_required": False,
        "posted_flags": [False] * len(comments),
    }

    async with async_playwright() as p:
        browser, page = await _new_page(p)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)

            pw_res = await _maybe_enter_password(page, password)
            if not pw_res["ok"]:
                result["error"] = pw_res["error"]
                result["password_required"] = pw_res["password_required"]
                result["failed"] = len(comments)
                return result

            # Wait for the SPA to render video + composer (up to ~30s)
            ready = False
            for _ in range(20):
                try:
                    has_video = await page.query_selector(
                        "video[data-testid='video-player']"
                    )
                    has_composer = await page.query_selector(
                        "[data-testid=create-comment-comment-composer]"
                    )
                    if has_video and has_composer:
                        ready = True
                        break
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
            if not ready:
                result["error"] = "Video / comment composer did not load."
                result["failed"] = len(comments)
                return result

            identity_saved = False

            for idx, c in enumerate(comments):
                try:
                    ts = c.get("timestamp_sec", 0.0)
                    if ts is None or ts < 0:
                        ts = 0.0
                    await _seek(page, ts)
                    ok = await _type_and_submit(page, c["text"])
                    if not ok:
                        result["failed"] += 1
                        continue

                    # The very first submit triggers the guest identity dialog.
                    if not identity_saved:
                        saved = await _accept_guest_identity_if_prompted(page)
                        identity_saved = saved or identity_saved

                    # Small wait + visual hint that submission completed:
                    # the composer should clear and a new comment row appears.
                    await page.wait_for_timeout(900)
                    result["posted"] += 1
                    result["posted_flags"][idx] = True
                except Exception as exc:
                    logger.warning("Comment #%s failed: %s", idx, exc)
                    result["failed"] += 1

            return result
        except Exception as exc:
            logger.exception("submit_guest_comments error: %s", exc)
            result["error"] = str(exc)
            result["failed"] = len(comments) - result["posted"]
            return result
        finally:
            await browser.close()


# ---- video download (kept identical to previous implementation) ----
async def download_video(url: str, dest_path: str) -> bool:
    try:
        async with httpx.AsyncClient(
            timeout=180.0, follow_redirects=True
        ) as client:
            async with client.stream("GET", url) as r:
                if r.status_code != 200:
                    return False
                with open(dest_path, "wb") as f:
                    async for chunk in r.aiter_bytes(1024 * 1024):
                        f.write(chunk)
        return True
    except Exception as exc:
        logger.exception("download_video error: %s", exc)
        return False


def is_share_link(url: str) -> bool:
    if not url:
        return False
    return bool(
        re.search(r"(f\.io/|next\.frame\.io/share/)", url, re.IGNORECASE)
    )
