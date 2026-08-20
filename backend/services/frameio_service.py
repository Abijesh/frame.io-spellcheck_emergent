"""Frame.io public share interaction via Playwright (guest mode)."""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GUEST_NAME = "Proof.io"
GUEST_EMAIL = "qa@proof.io"
ASSET_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Empirically measured from Frame.io player keyboard shortcuts (2026 UI):
COARSE_STEP_SEC = 1.0 / 3.0   # Shift+ArrowRight ≈ 0.333 s
FINE_STEP_SEC = 1.0 / 30.0    # ArrowRight ≈ one frame (~0.033 s at 30 fps)


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


def is_share_link(url: str) -> bool:
    if not url:
        return False
    return bool(re.search(r"(f\.io/|next\.frame\.io/share/)", url, re.IGNORECASE))


async def _maybe_enter_password(page, password: Optional[str]) -> dict:
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
    await page.keyboard.press("Enter")
    try:
        await page.wait_for_selector(
            "video[data-testid='video-player']", timeout=10000
        )
    except Exception:
        if await page.query_selector("input[type=password]"):
            return {"ok": False, "password_required": True, "error": "Wrong password."}
    return {"ok": True, "password_required": False, "error": None}


async def _wait_for_player_and_composer(page, max_seconds: int = 25) -> bool:
    for _ in range(max_seconds * 2):
        v = await page.query_selector("video[data-testid='video-player']")
        c = await page.query_selector("[data-testid=create-comment-comment-composer]")
        if v and c:
            return True
        await page.wait_for_timeout(500)
    return False


async def _dismiss_play_overlay(page) -> None:
    """Click the big play overlay so the player becomes interactive, then pause."""
    try:
        await page.locator("[data-testid=overlay-play-button]").click(timeout=4000)
        await page.wait_for_timeout(500)
        await page.keyboard.press("k")  # pause
        await page.wait_for_timeout(250)
    except Exception:
        pass


async def _current_time(page) -> float:
    try:
        return float(await page.evaluate(
            "() => document.querySelector(\"video[data-testid='video-player']\").currentTime"
        ))
    except Exception:
        return 0.0


async def _seek_forward(page, delta_sec: float) -> None:
    """Step the playhead forward by delta_sec using Frame.io's keyboard shortcuts.

    Shift+ArrowRight ≈ 0.333s, ArrowRight ≈ 1 frame (~0.033s).
    We focus the playhead slider so the keys go to the player, not the page.
    """
    if delta_sec <= 0:
        return
    try:
        await page.locator("[data-testid=playhead]").focus()
    except Exception:
        # fall back: focus the video element
        try:
            await page.locator("video[data-testid='video-player']").focus()
        except Exception:
            pass

    coarse = int(delta_sec // COARSE_STEP_SEC)
    remainder = delta_sec - coarse * COARSE_STEP_SEC
    fine = int(round(remainder / FINE_STEP_SEC))
    for _ in range(coarse):
        await page.keyboard.press("Shift+ArrowRight")
    for _ in range(fine):
        await page.keyboard.press("ArrowRight")
    await page.wait_for_timeout(120)


async def _seek_to_start(page) -> None:
    """Reset playhead to 0. Home doesn't work; we step backwards aggressively."""
    try:
        await page.locator("[data-testid=playhead]").focus()
    except Exception:
        pass
    # 200 Shift+Left presses is enough to rewind any short clip to 0
    for _ in range(200):
        await page.keyboard.press("Shift+ArrowLeft")
    await page.wait_for_timeout(150)


async def resolve_share_video(url: str, password: Optional[str] = None) -> dict:
    """Open the share page, fill password if needed, return signed video URL."""
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
            await page.wait_for_timeout(3000)

            pw_res = await _maybe_enter_password(page, password)
            if not pw_res["ok"]:
                out["password_required"] = pw_res["password_required"]
                out["error"] = pw_res["error"]
                return out

            video_src = None
            for _ in range(20):
                try:
                    video_src = await page.eval_on_selector(
                        "video[data-testid='video-player'], video",
                        "v => v && (v.currentSrc || v.src)",
                    )
                except Exception:
                    video_src = None
                if video_src:
                    break
                await page.wait_for_timeout(1000)

            if not video_src:
                out["error"] = "Video player did not load on the share page."
                return out

            out["video_url"] = video_src
            ids = ASSET_ID_RE.findall(page.url)
            out["file_id"] = ids[-1] if ids else None
            if not out["file_id"]:
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
        await page.locator("[role=dialog] button:has-text('Save')").first.click()
        await page.wait_for_timeout(900)
        return True
    except Exception as exc:
        logger.warning("guest identity dialog failed: %s", exc)
        return False


async def _type_and_submit(page, text: str) -> bool:
    composer = page.locator("[data-testid=create-comment-comment-composer]")
    try:
        await composer.click(timeout=4000)
    except Exception:
        return False
    await page.keyboard.type(text, delay=4)
    submit = page.locator("[data-testid=composer-submit-button]")
    try:
        await submit.click(timeout=3000)
        return True
    except Exception:
        await page.keyboard.press("Control+Enter")
        return True


async def submit_guest_comments(
    url: str,
    password: Optional[str],
    comments: list,
) -> dict:
    """`comments`: list of {timestamp_sec: float, text: str}.

    Posts in ascending-timestamp order so we only ever seek forward.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"posted": 0, "failed": len(comments), "error": "playwright not installed"}

    n = len(comments)
    result = {
        "posted": 0,
        "failed": 0,
        "error": None,
        "password_required": False,
        "posted_flags": [False] * n,
    }

    # Sort by timestamp (negative = transcript, treat as 0) but keep original index
    indexed = list(enumerate(comments))
    indexed.sort(key=lambda x: max(float(x[1].get("timestamp_sec") or 0.0), 0.0))

    async with async_playwright() as p:
        browser, page = await _new_page(p)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            pw_res = await _maybe_enter_password(page, password)
            if not pw_res["ok"]:
                result["error"] = pw_res["error"]
                result["password_required"] = pw_res["password_required"]
                result["failed"] = n
                return result

            if not await _wait_for_player_and_composer(page):
                result["error"] = "Video or comment composer did not load."
                result["failed"] = n
                return result

            await _dismiss_play_overlay(page)

            current_t = await _current_time(page)
            identity_saved = False

            for orig_idx, c in indexed:
                try:
                    target = max(float(c.get("timestamp_sec") or 0.0), 0.0)
                    # Seek forward to target. If we've already passed it
                    # (shouldn't, since sorted) just skip seeking.
                    delta = target - current_t
                    if delta > 0:
                        await _seek_forward(page, delta)
                        current_t = await _current_time(page)

                    ok = await _type_and_submit(page, c["text"])
                    if not ok:
                        result["failed"] += 1
                        continue

                    if not identity_saved:
                        if await _accept_guest_identity_if_prompted(page):
                            identity_saved = True

                    await page.wait_for_timeout(450)
                    result["posted"] += 1
                    result["posted_flags"][orig_idx] = True
                except Exception as exc:
                    logger.warning("Comment #%s failed: %s", orig_idx, exc)
                    result["failed"] += 1

            return result
        except Exception as exc:
            logger.exception("submit_guest_comments error: %s", exc)
            result["error"] = str(exc)
            result["failed"] = n - result["posted"]
            return result
        finally:
            await browser.close()


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
