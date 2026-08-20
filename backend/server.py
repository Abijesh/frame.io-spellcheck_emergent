"""Frame.io QA backend.

Two ways to reach Frame.io, chosen per-analysis based on whether the caller
has a connected Frame.io account (session cookie):

  Connected  (OAuth, official V4 API): real download_url via the Files API,
             comments posted via the real Comments API with frame-accurate
             timestamps. Requires the file to be in a project this identity
             is a member of -- the official API has no way to resolve an
             arbitrary public share link outside that, so...
  Anonymous  (Playwright, guest mode): headless Chromium opens the share,
             scrapes the signed video URL, and posts comments by simulating
             keyboard seeks + typing as a guest named 'Spellchecker'. This is
             the only way to handle a share link from someone else's account.

Both paths first do a lightweight page-load of the share link to resolve its
file UUID (frameio_service.resolve_share_video) -- the official API has no
share-lookup endpoint either, so this step is unavoidable either way.

Processing pipeline (shared by both paths):
  1. Resolve the share link (or use the uploaded video directly).
  2. ffmpeg densely samples frames every FRAME_SAMPLE_INTERVAL seconds; a
     local OCR pass (ocr_service) finds which frames have text and merges
     consecutive matching frames into distinct on-screen text instances.
  3. Gemini 3 Flash reads + spellchecks the clearest frame of each text
     instance once (not once per sample), and a thumbnail is cropped from it.
  4. Optional transcript-level pass.
  5. Post each issue back to Frame.io via whichever path applies.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import RedirectResponse
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.middleware.cors import CORSMiddleware

from models import Analysis, FrameioSession, Issue
from services import ai_service, frameio_api, frameio_oauth, frameio_service, ocr_service, video_service

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("frameio-qa")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]
analyses_col = db["analyses"]
frameio_sessions_col = db["frameio_sessions"]

FRAME_INTERVAL = float(os.environ.get("FRAME_SAMPLE_INTERVAL", "0.5"))
# Product scope is spelling/grammar only for now. The prompt already asks
# Gemini not to flag these, but that's not a guarantee -- drop them here too
# so a model slip-up never reaches a user.
SKIPPED_ISSUE_TYPES = {"punctuation", "capitalization"}
# Soft-deleted analyses (and their embedded base64 thumbnails) are kept
# recoverable for this long, then permanently purged -- see _purge_loop.
PURGE_AFTER_DAYS = int(os.environ.get("PURGE_AFTER_DAYS", "30"))
PURGE_INTERVAL_SEC = 24 * 60 * 60
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
OAUTH_STATE_COOKIE = "fio_oauth_state"
SESSION_COOKIE = "fio_session"

app = FastAPI(title="Frame.io QA")
api = APIRouter(prefix="/api")


# ---------------- helpers ----------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _save(analysis: Analysis) -> None:
    analysis.updated_at = _now_iso()
    await analyses_col.replace_one(
        {"id": analysis.id}, analysis.model_dump(), upsert=True
    )


async def _set_status(analysis_id: str, **patch) -> None:
    patch["updated_at"] = _now_iso()
    await analyses_col.update_one({"id": analysis_id}, {"$set": patch})


async def _load(analysis_id: str) -> Optional[Analysis]:
    doc = await analyses_col.find_one({"id": analysis_id}, {"_id": 0})
    return Analysis(**doc) if doc else None


async def _new_frameio_session(tokens: dict) -> str:
    """Store OAuth tokens server-side; only the opaque session id goes to the
    browser (as an httpOnly cookie), so the tokens themselves never leave
    the backend."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    ).isoformat()
    session = FrameioSession(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=expires_at,
    )
    try:
        session.account_ids = await frameio_api.list_account_ids(session.access_token)
    except frameio_api.FrameioApiError as exc:
        logger.warning("Could not resolve Frame.io accounts at connect time: %s", exc)
    await frameio_sessions_col.insert_one(session.model_dump())
    return session.id


async def _get_valid_frameio_token(session_id: Optional[str]) -> Optional[FrameioSession]:
    """Returns a session with a live access_token (refreshing if needed), or
    None if not connected / the session is gone."""
    if not session_id:
        return None
    doc = await frameio_sessions_col.find_one({"id": session_id}, {"_id": 0})
    if not doc:
        return None
    session = FrameioSession(**doc)
    expires_at = datetime.fromisoformat(session.expires_at)
    if expires_at > datetime.now(timezone.utc) + timedelta(minutes=2):
        return session
    try:
        tokens = await frameio_oauth.refresh_access_token(session.refresh_token)
    except Exception as exc:
        logger.warning("Frame.io token refresh failed for session %s: %s", session_id, exc)
        return None
    session.access_token = tokens["access_token"]
    session.refresh_token = tokens.get("refresh_token", session.refresh_token)
    session.expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    ).isoformat()
    await frameio_sessions_col.update_one(
        {"id": session_id},
        {"$set": {
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expires_at": session.expires_at,
        }},
    )
    return session


async def _resolve_reachable_account_id(
    fio_session: Optional[FrameioSession], file_id: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Tries every Frame.io account this identity belongs to (see the comment
    on FrameioSession.account_ids -- there can be several, with no "default")
    and returns the (account_id, download_url) of the first one that can
    actually see this file, or (None, None) if none can."""
    if not fio_session or not fio_session.account_ids or not file_id:
        return None, None
    for account_id in fio_session.account_ids:
        try:
            url = await frameio_api.get_file_download_url(
                fio_session.access_token, account_id, file_id
            )
        except frameio_api.FrameioApiError as exc:
            logger.warning("Account %s file lookup failed: %s", account_id, exc)
            continue
        if url:
            return account_id, url
    return None, None


def _severity_for(err_type: str) -> str:
    return {
        "spelling": "high",
        "grammar": "high",
        "punctuation": "low",
        "capitalization": "medium",
        "contrast": "medium",
    }.get(err_type, "medium")


async def _post_issues_to_frameio(
    analysis: Analysis, issues: List[Issue], fio_session: Optional[FrameioSession]
) -> dict:
    """Posts `issues` (must be a subset of analysis.issues, same objects, so
    mutations here are visible to the caller) to Frame.io: official API if
    this identity can reach the file, guest Playwright flow otherwise.
    Shared by the auto-post pipeline and both manual-post endpoints so the
    routing logic only lives in one place. Returns {"posted": int, "error":
    str|None}."""
    if not issues:
        return {"posted": 0, "error": None}

    account_id, _ = await _resolve_reachable_account_id(
        fio_session, analysis.frameio_asset_id
    )

    if account_id:
        posted = 0
        for issue in issues:
            frame = frameio_api.sec_to_frame(issue.timestamp_sec, analysis.video_fps)
            try:
                resp = await frameio_api.create_comment(
                    fio_session.access_token,
                    account_id,
                    analysis.frameio_asset_id,
                    _format_comment(issue),
                    frame,
                )
                issue.posted_to_frameio = True
                issue.posted_via = "official_api"
                issue.frameio_comment_id = resp.get("id")
                posted += 1
            except frameio_api.FrameioApiError as exc:
                logger.warning("Official comment post failed for issue %s: %s", issue.id, exc)
        error = "Frame.io API rejected all comments." if posted == 0 else None
        return {"posted": posted, "error": error}

    if not analysis.frameio_url or not frameio_service.is_share_link(analysis.frameio_url):
        return {
            "posted": 0,
            "error": "This file isn't reachable via your connected Frame.io "
            "account, and it isn't a share link either, so there's no way "
            "to post comments to it.",
        }

    payloads = [
        {"timestamp_sec": i.timestamp_sec, "text": _format_comment(i)} for i in issues
    ]
    result = await frameio_service.submit_guest_comments(
        analysis.frameio_url, analysis.password, payloads
    )
    flags = result.get("posted_flags") or []
    posted = 0
    for idx, issue in enumerate(issues):
        if idx < len(flags) and flags[idx]:
            issue.posted_to_frameio = True
            issue.posted_via = "guest"
            posted += 1
    error = result.get("error")
    if not error and result.get("failed", 0) > 0 and posted == 0:
        error = (
            "Frame.io rejected all comments. The share owner may have "
            "disabled commenting on this link."
        )
    return {"posted": posted, "error": error}


def _format_comment(issue: Issue) -> str:
    label = {
        "spelling": "Spelling",
        "grammar": "Grammar",
        "punctuation": "Punctuation",
        "capitalization": "Capitalization",
        "contrast": "Contrast",
    }.get(issue.type, "Issue")
    parts = [f"[{label}]"]
    if issue.original:
        parts.append(f'"{issue.original}"')
    if issue.suggestion:
        parts.append(f"→ \"{issue.suggestion}\"")
    if issue.explanation:
        parts.append(f"— {issue.explanation}")
    return " ".join(parts)


# ---------------- background pipeline ----------------
async def _run_pipeline(
    analysis_id: str,
    video_local_path: Optional[str] = None,
    frameio_session_id: Optional[str] = None,
) -> None:
    workdir = tempfile.mkdtemp(prefix=f"frameio_{analysis_id}_")
    stage_t0 = time.monotonic()

    def _log_stage(label: str) -> None:
        nonlocal stage_t0
        now = time.monotonic()
        logger.info("Pipeline %s: %s took %.1fs", analysis_id, label, now - stage_t0)
        stage_t0 = now

    try:
        analysis = await _load(analysis_id)
        if not analysis:
            return

        allowlist_terms = ai_service.parse_allowlist(analysis.allowlist)
        allowlist_set = {t.lower() for t in allowlist_terms}

        fio_session = await _get_valid_frameio_token(frameio_session_id)
        # Set only if the official API actually resolved this file (i.e. it's
        # in a project the connected account belongs to) -- gates whether
        # comment-posting attempts the official API at all, so we don't waste
        # calls hitting 403s for a video that isn't ours before falling back.
        official_file_reachable = False

        video_path = video_local_path
        if not video_path:
            url = analysis.frameio_url or ""
            if not frameio_service.is_share_link(url):
                await _set_status(
                    analysis_id,
                    status="failed",
                    error="Paste a Frame.io share link (f.io/... or "
                    "next.frame.io/share/...) or upload a video file.",
                    progress=100,
                )
                return

            await _set_status(
                analysis_id,
                status="downloading",
                progress=5,
                message="Opening Frame.io share...",
            )
            info = await frameio_service.resolve_share_video(url, analysis.password)
            if info.get("password_required"):
                await _set_status(
                    analysis_id,
                    status="failed",
                    error=info.get("error") or "Password required.",
                    password_required=True,
                    progress=100,
                )
                return
            if not info.get("video_url"):
                await _set_status(
                    analysis_id,
                    status="failed",
                    error=info.get("error")
                    or "Could not read the video from this share. The share may "
                    "be expired or restricted.",
                    progress=100,
                )
                return
            if info.get("file_id"):
                await _set_status(analysis_id, frameio_asset_id=info["file_id"])

            video_path = os.path.join(workdir, "input.mp4")
            await _set_status(
                analysis_id, message="Downloading video...", progress=15
            )

            download_url = info["video_url"]
            if fio_session and info.get("file_id"):
                _, official_url = await _resolve_reachable_account_id(
                    fio_session, info["file_id"]
                )
                if official_url:
                    download_url = official_url
                    official_file_reachable = True

            ok = await frameio_service.download_video(download_url, video_path)
            if not ok:
                await _set_status(
                    analysis_id,
                    status="failed",
                    error="Failed to download the video.",
                    progress=100,
                )
                return

        _log_stage("resolving + downloading the video")

        duration = video_service.get_duration(video_path)
        fps = video_service.get_fps(video_path)
        await _set_status(
            analysis_id,
            status="extracting",
            duration_sec=duration,
            video_fps=fps,
            message="Extracting frames...",
            progress=20,
        )

        frames_dir = os.path.join(workdir, "frames")
        frames = await video_service.extract_frames(
            video_path, frames_dir, FRAME_INTERVAL
        )
        _log_stage(f"ffmpeg frame extraction ({len(frames)} frames)")

        # Cheap local OCR pass: figure out which frames have text at all, and
        # merge consecutive matching frames into one instance per distinct
        # piece of on-screen text so Gemini only sees it once.
        await _set_status(
            analysis_id,
            total_frames=len(frames),
            message=f"Scanning {len(frames)} frames for on-screen text...",
            progress=25,
        )
        # Sequential: a single GPU-backed EasyOCR reader, one call at a time.
        # (Concurrent threads gave no speedup on CPU and add real risk on a
        # single shared CUDA context, so not worth it now that GPU alone
        # makes each call fast.)
        frame_results: List[Tuple[float, List[dict]]] = []
        for i, (ts, fpath) in enumerate(frames):
            hits = await asyncio.to_thread(ocr_service.ocr_frame, fpath)
            frame_results.append((ts, hits))
            if i % 5 == 0 or i == len(frames) - 1:
                await _set_status(
                    analysis_id,
                    progress=25 + int(20 * (i + 1) / max(len(frames), 1)),
                    message=f"Scanning frame {i + 1}/{len(frames)} for text...",
                )
        instances = ocr_service.merge_instances(frame_results)
        _log_stage(
            f"local OCR scan ({len(frames)} frames -> {len(instances)} "
            "text instance(s) after noise filtering)"
        )

        await _set_status(
            analysis_id,
            status="analyzing",
            total_frames=len(instances),
            message=f"Found {len(instances)} on-screen text instance(s), "
            f"checking with Gemini 3 Flash...",
            progress=45,
        )

        all_issues: List[Issue] = []
        completed = 0
        unchecked_count = 0
        quota_state = {"exceeded": False}
        sem = asyncio.Semaphore(3)  # cap concurrent Gemini calls

        async def _analyze_one(inst: dict):
            _, fpath = frames[inst["frame_index"]]
            errs: List[dict] = []
            checked = False
            if quota_state["exceeded"]:
                pass  # already know every further call will 429 -- don't bother
            else:
                try:
                    async with sem:
                        errs = await ai_service.analyze_frame(fpath, allowlist_terms)
                    checked = True
                except ai_service.GeminiQuotaExceeded:
                    quota_state["exceeded"] = True
            thumb = await asyncio.to_thread(
                ocr_service.crop_thumbnail, fpath, inst["ocr_results"]
            )
            contrast_hits: List[dict] = []
            if analysis.check_contrast:
                contrast_hits = await asyncio.to_thread(
                    ocr_service.check_contrast, fpath, inst["ocr_results"]
                )
            return inst, errs, thumb, contrast_hits, checked

        async def _run_with_progress():
            nonlocal completed, unchecked_count
            tasks = [asyncio.create_task(_analyze_one(inst)) for inst in instances]
            for fut in asyncio.as_completed(tasks):
                inst, errs, thumb, contrast_hits, checked = await fut
                if not checked:
                    unchecked_count += 1
                thumb_b64 = base64.b64encode(thumb).decode() if thumb else None
                for e in errs:
                    if e.get("type") in SKIPPED_ISSUE_TYPES:
                        continue
                    if (e.get("original") or "").strip().lower() in allowlist_set:
                        continue
                    all_issues.append(
                        Issue(
                            timestamp_sec=float(inst["start"]),
                            end_sec=float(inst["end"]),
                            type=e.get("type", "spelling"),
                            original=e.get("original", ""),
                            suggestion=e.get("suggestion", ""),
                            explanation=e.get("explanation", ""),
                            source_text=e.get("source_text", ""),
                            severity=_severity_for(e.get("type", "spelling")),
                            thumbnail_b64=thumb_b64,
                        )
                    )
                if contrast_hits:
                    # Only the worst offender per instance -- one contrast
                    # issue per on-screen text instance, same granularity as
                    # the spelling/grammar checks above.
                    worst = min(contrast_hits, key=lambda c: c["ratio"])
                    all_issues.append(
                        Issue(
                            timestamp_sec=float(inst["start"]),
                            end_sec=float(inst["end"]),
                            type="contrast",
                            original=worst["text"],
                            source_text=worst["text"],
                            explanation=(
                                "This text blends into what's behind it, so it's "
                                "hard to read at a glance. Try a bolder/lighter "
                                "text color, an outline, or a drop shadow/"
                                "background behind it. (Measured contrast "
                                f"{worst['ratio']}:1 — accessibility guidelines "
                                f"call for at least {worst['threshold']}:1.)"
                            ),
                            severity=_severity_for("contrast"),
                            thumbnail_b64=thumb_b64,
                        )
                    )
                completed += 1
                await analyses_col.update_one(
                    {"id": analysis_id},
                    {
                        "$set": {
                            "analyzed_frames": completed,
                            "progress": 45 + int(35 * completed / max(len(instances), 1)),
                            "issues": [i.model_dump() for i in all_issues],
                            "message": f"Checked {completed}/{len(instances)} text instances",
                        }
                    },
                )

        await _run_with_progress()
        _log_stage(
            f"Gemini + contrast analysis ({len(instances)} instance(s), "
            f"{unchecked_count} unchecked)"
        )

        # transcript pass (no timestamp)
        analysis = await _load(analysis_id)
        if quota_state["exceeded"]:
            logger.info(
                "Pipeline %s: skipping transcript check, Gemini quota already "
                "exceeded this run", analysis_id,
            )
        elif analysis and analysis.transcript:
            await _set_status(
                analysis_id, message="Checking transcript...", progress=82
            )
            try:
                transcript_errs = await ai_service.analyze_transcript(
                    analysis.transcript, allowlist_terms
                )
            except ai_service.GeminiQuotaExceeded:
                quota_state["exceeded"] = True
                transcript_errs = []
            for e in transcript_errs:
                if e.get("type") in SKIPPED_ISSUE_TYPES:
                    continue
                if (e.get("original") or "").strip().lower() in allowlist_set:
                    continue
                all_issues.append(
                    Issue(
                        timestamp_sec=-1.0,
                        type=e.get("type", "grammar"),
                        original=e.get("original", ""),
                        suggestion=e.get("suggestion", ""),
                        explanation=e.get("explanation", ""),
                        source_text=e.get("context", ""),
                        severity=_severity_for(e.get("type", "grammar")),
                    )
                )

        # Post comments: official API if this identity can reach the file
        # (member of its project), guest Playwright flow otherwise -- see
        # _post_issues_to_frameio for the routing logic itself.
        analysis = await _load(analysis_id)
        posted = 0
        post_err_msg: Optional[str] = None
        postable: List[Issue] = []
        if (
            analysis
            and analysis.auto_post
            and all_issues
            and (
                (fio_session and fio_session.account_ids and analysis.frameio_asset_id)
                or (analysis.frameio_url and frameio_service.is_share_link(analysis.frameio_url))
            )
        ):
            postable = [i for i in all_issues if i.timestamp_sec >= 0]
            await _set_status(
                analysis_id,
                status="posting",
                progress=85,
                message=f"Posting {len(postable)} comments to Frame.io...",
            )
            result = await _post_issues_to_frameio(analysis, postable, fio_session)
            posted = result["posted"]
            post_err_msg = result["error"]

        posted_via_official = any(i.posted_via == "official_api" for i in postable)
        quota_note = (
            f" {unchecked_count} on-screen text instance(s) could not be "
            "checked -- Gemini's quota ran out partway through. Issues "
            "found before that point are real and kept; the unchecked "
            "instances are simply unknown, not confirmed clean."
            if quota_state["exceeded"]
            else ""
        )
        await analyses_col.update_one(
            {"id": analysis_id},
            {
                "$set": {
                    "status": "done",
                    "progress": 100,
                    "issues": [i.model_dump() for i in all_issues],
                    "posted_count": posted,
                    "post_error": post_err_msg,
                    "quota_exceeded": quota_state["exceeded"],
                    "unchecked_instances": unchecked_count,
                    "message": f"Done. {len(all_issues)} issues found, "
                    f"{posted} posted to Frame.io"
                    + (" via API." if posted_via_official else " as Spellchecker.")
                    + quota_note,
                }
            },
        )
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        await _set_status(
            analysis_id, status="failed", error=str(exc), progress=100
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------- routes ----------------
@api.get("/")
async def root():
    return {"service": "Frame.io QA", "status": "ok"}


@api.get("/config")
async def config():
    return {
        "llm_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "frame_interval": FRAME_INTERVAL,
        "guest_name": frameio_service.GUEST_NAME,
        "frameio_oauth_configured": frameio_oauth.is_configured(),
    }


# ---------------- Frame.io OAuth ----------------
@api.get("/frameio/oauth/authorize")
async def frameio_oauth_authorize():
    if not frameio_oauth.is_configured():
        raise HTTPException(500, "Frame.io OAuth is not configured on this server.")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(frameio_oauth.build_authorize_url(state))
    resp.set_cookie(
        OAUTH_STATE_COOKIE, state,
        httponly=True, secure=True, samesite="lax", max_age=300,
    )
    return resp


@api.get("/frameio/oauth/callback")
async def frameio_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if error or not code or not state or state != expected_state:
        return RedirectResponse(f"{FRONTEND_URL}/?frameio_connect=error")

    try:
        tokens = await frameio_oauth.exchange_code(code)
        session_id = await _new_frameio_session(tokens)
    except Exception as exc:
        logger.exception("Frame.io OAuth token exchange failed: %s", exc)
        return RedirectResponse(f"{FRONTEND_URL}/?frameio_connect=error")

    resp = RedirectResponse(f"{FRONTEND_URL}/?frameio_connect=success")
    resp.delete_cookie(OAUTH_STATE_COOKIE)
    # None (not Lax): this cookie has to survive fetch/XHR calls the frontend
    # makes from its own origin (localhost:3000) to this one (localhost:8000)
    # -- different scheme makes them cross-site under "schemeful same-site",
    # which Lax blocks for subresource requests. None+Secure is the standard
    # fix for a genuinely cross-origin frontend/backend pair like this one.
    resp.set_cookie(
        SESSION_COOKIE, session_id,
        httponly=True, secure=True, samesite="none", max_age=60 * 60 * 24 * 30,
    )
    return resp


@api.get("/frameio/oauth/status")
async def frameio_oauth_status(
    fio_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    session = await _get_valid_frameio_token(fio_session)
    return {"connected": bool(session and session.account_ids)}


@api.post("/frameio/oauth/disconnect")
async def frameio_oauth_disconnect(
    response: Response,
    fio_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    if fio_session:
        await frameio_sessions_col.delete_one({"id": fio_session})
    response.delete_cookie(SESSION_COOKIE)
    return {"disconnected": True}


@api.post("/analyses")
async def create_analysis(
    background: BackgroundTasks,
    frameio_url: Optional[str] = Form(None),
    transcript: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    auto_post: bool = Form(True),
    check_contrast: bool = Form(False),
    allowlist: Optional[str] = Form(None),
    video: Optional[UploadFile] = File(None),
    fio_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    if not frameio_url and not video:
        raise HTTPException(
            status_code=400,
            detail="Provide either a Frame.io share link or a video file.",
        )

    analysis = Analysis(
        frameio_url=frameio_url,
        transcript=transcript,
        password=password,
        auto_post=auto_post,
        check_contrast=check_contrast,
        allowlist=allowlist,
        video_filename=video.filename if video else None,
    )
    await _save(analysis)

    video_local_path: Optional[str] = None
    if video:
        tmpdir = tempfile.mkdtemp(prefix=f"upload_{analysis.id}_")
        video_local_path = os.path.join(tmpdir, video.filename or "input.mp4")
        with open(video_local_path, "wb") as out:
            shutil.copyfileobj(video.file, out)

    background.add_task(_run_pipeline, analysis.id, video_local_path, fio_session)
    d = analysis.model_dump()
    d.pop("password", None)
    return d


@api.get("/analyses")
async def list_analyses():
    cursor = (
        analyses_col.find({"deleted": {"$ne": True}}, {"_id": 0})
        .sort("created_at", -1)
        .limit(50)
    )
    items = await cursor.to_list(length=50)
    for it in items:
        it["issue_count"] = len(it.get("issues") or [])
        it["issues"] = []
        it.pop("password", None)
    return items


@api.get("/analyses/{analysis_id}")
async def get_analysis(analysis_id: str):
    a = await _load(analysis_id)
    if not a:
        raise HTTPException(404, "Not found")
    d = a.model_dump()
    d.pop("password", None)
    return d


@api.post("/analyses/{analysis_id}/issues/{issue_id}/post")
async def post_single_issue(
    analysis_id: str,
    issue_id: str,
    fio_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    """Post one specific issue to Frame.io -- official API if connected and
    this identity can reach the file, guest comment otherwise."""
    a = await _load(analysis_id)
    if not a:
        raise HTTPException(404, "Not found")
    idx = next(
        (i for i, x in enumerate(a.issues) if x.id == issue_id), None
    )
    if idx is None:
        raise HTTPException(404, "Issue not found")
    issue = a.issues[idx]
    if issue.posted_to_frameio:
        return {"posted": False, "already": True}

    session = await _get_valid_frameio_token(fio_session)
    result = await _post_issues_to_frameio(a, [issue], session)
    if result["posted"]:
        a.posted_count = sum(1 for i in a.issues if i.posted_to_frameio)
        await _save(a)
        return {"posted": True, "posted_via": issue.posted_via}
    return {
        "posted": False,
        "error": result["error"] or "Frame.io rejected the comment.",
    }


@api.post("/analyses/{analysis_id}/post")
async def manual_post(
    analysis_id: str,
    fio_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    a = await _load(analysis_id)
    if not a:
        raise HTTPException(404, "Not found")

    unposted = [x for x in a.issues if not x.posted_to_frameio]
    if not unposted:
        return {"posted": 0, "total_posted": a.posted_count}

    session = await _get_valid_frameio_token(fio_session)
    result = await _post_issues_to_frameio(a, unposted, session)
    a.posted_count = sum(1 for i in a.issues if i.posted_to_frameio)
    await _save(a)
    return {
        "posted": result["posted"],
        "total_posted": a.posted_count,
        "error": result["error"],
    }


@api.get("/analyses/{analysis_id}/csv")
async def export_csv(analysis_id: str):
    from fastapi.responses import StreamingResponse
    import csv as csv_mod
    import io

    a = await _load(analysis_id)
    if not a:
        raise HTTPException(404, "Not found")

    buf = io.StringIO()
    w = csv_mod.writer(buf)
    w.writerow([
        "Timestamp (mm:ss)",
        "Seconds",
        "Ends (mm:ss)",
        "Type",
        "Severity",
        "Original",
        "Suggestion",
        "Explanation",
        "Source text",
        "Posted to Frame.io",
    ])
    for i in sorted(a.issues, key=lambda x: x.timestamp_sec):
        ts = i.timestamp_sec
        mm_ss = (
            "Script"
            if ts < 0
            else f"{int(ts // 60):02d}:{int(ts % 60):02d}"
        )
        end_mm_ss = (
            ""
            if ts < 0 or i.end_sec is None
            else f"{int(i.end_sec // 60):02d}:{int(i.end_sec % 60):02d}"
        )
        w.writerow([
            mm_ss,
            "" if ts < 0 else f"{ts:.2f}",
            end_mm_ss,
            i.type,
            i.severity,
            i.original,
            i.suggestion,
            i.explanation,
            i.source_text,
            "yes" if i.posted_to_frameio else "no",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="proofio-{analysis_id[:8]}.csv"'
        },
    )


@api.delete("/analyses/{analysis_id}")
async def delete_analysis(analysis_id: str):
    """Soft delete: hides the analysis from history without erasing it.
    Permanently purged after PURGE_AFTER_DAYS by the background task below."""
    res = await analyses_col.update_one(
        {"id": analysis_id}, {"$set": {"deleted": True, "deleted_at": _now_iso()}}
    )
    return {"deleted": res.modified_count > 0}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def ensure_runtime_deps():
    """Self-heal Playwright + ffmpeg on first boot so pod recycles don't
    silently break the analysis pipeline."""
    import shutil as _sh
    import subprocess

    # ffmpeg (OS-level, comes from apt)
    if not _sh.which("ffmpeg"):
        try:
            subprocess.run(
                ["apt-get", "install", "-y", "ffmpeg"],
                check=False, timeout=120,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.warning("ffmpeg install at startup failed: %s", exc)

    # Playwright chromium browser
    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    chromium_ok = False
    if pw_path and os.path.isdir(pw_path):
        for entry in os.listdir(pw_path):
            if entry.startswith("chromium_headless_shell-"):
                shell = os.path.join(
                    pw_path, entry, "chrome-linux", "headless_shell"
                )
                if os.path.exists(shell):
                    chromium_ok = True
                    break
    if not chromium_ok:
        try:
            env = {**os.environ}
            subprocess.run(
                ["python3", "-m", "playwright", "install", "chromium"],
                check=False, timeout=300, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info("Playwright chromium installed at startup")
        except Exception as exc:
            logger.warning("Playwright install at startup failed: %s", exc)


async def _purge_old_deleted() -> int:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=PURGE_AFTER_DAYS)
    ).isoformat()
    res = await analyses_col.delete_many(
        {"deleted": True, "deleted_at": {"$lt": cutoff}}
    )
    if res.deleted_count:
        logger.info("Purged %d soft-deleted analyses older than %d days",
                     res.deleted_count, PURGE_AFTER_DAYS)
    return res.deleted_count


async def _purge_loop():
    while True:
        try:
            await _purge_old_deleted()
        except Exception as exc:
            logger.warning("Purge pass failed: %s", exc)
        await asyncio.sleep(PURGE_INTERVAL_SEC)


_purge_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def start_purge_loop():
    global _purge_task
    _purge_task = asyncio.create_task(_purge_loop())


@app.on_event("shutdown")
async def shutdown_db_client():
    if _purge_task:
        _purge_task.cancel()
    client.close()
