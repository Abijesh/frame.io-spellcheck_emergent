"""Frame.io QA backend.

Pipeline per analysis (run as a background task):
  1. Resolve Frame.io URL → asset id → signed video URL → download to /tmp.
     OR use uploaded video file.
  2. ffmpeg → extract one frame every FRAME_SAMPLE_INTERVAL seconds.
  3. Gemini 3 Flash on each frame → OCR + spelling/grammar issues.
  4. Optional transcript-level grammar pass.
  5. Auto-post each issue back to Frame.io as a comment with timestamp.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.middleware.cors import CORSMiddleware

from models import Analysis, Issue
from services import ai_service, frameio_service, video_service

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

FRAME_INTERVAL = float(os.environ.get("FRAME_SAMPLE_INTERVAL", "2"))

app = FastAPI(title="Frame.io QA")
api = APIRouter(prefix="/api")


# ---------------- helpers ----------------
async def _save(analysis: Analysis) -> None:
    analysis.updated_at = Analysis.model_fields["updated_at"].default_factory()  # type: ignore
    doc = analysis.model_dump()
    await analyses_col.replace_one({"id": analysis.id}, doc, upsert=True)


async def _set_status(analysis_id: str, **patch) -> None:
    patch["updated_at"] = Analysis.model_fields["updated_at"].default_factory()  # type: ignore
    await analyses_col.update_one({"id": analysis_id}, {"$set": patch})


async def _load(analysis_id: str) -> Optional[Analysis]:
    doc = await analyses_col.find_one({"id": analysis_id}, {"_id": 0})
    if not doc:
        return None
    return Analysis(**doc)


def _severity_for(err_type: str) -> str:
    return {
        "spelling": "high",
        "grammar": "high",
        "punctuation": "low",
        "capitalization": "medium",
    }.get(err_type, "medium")


# ---------------- background pipeline ----------------
async def _run_pipeline(analysis_id: str, video_local_path: Optional[str] = None) -> None:
    workdir = tempfile.mkdtemp(prefix=f"frameio_{analysis_id}_")
    try:
        analysis = await _load(analysis_id)
        if not analysis:
            return

        video_path = video_local_path
        if not video_path:
            url = analysis.frameio_url or ""
            # ---- Public share link path (f.io / next.frame.io/share) ----
            if frameio_service.is_share_link(url):
                await _set_status(
                    analysis_id,
                    status="downloading",
                    progress=5,
                    message="Resolving Frame.io share link...",
                )
                share_info = await frameio_service.resolve_share_video(url)
                if not share_info or not share_info.get("video_url"):
                    await _set_status(
                        analysis_id,
                        status="failed",
                        error="Could not extract the video from this Frame.io "
                        "share link. The share may be password-protected or "
                        "expired.",
                        progress=100,
                    )
                    return
                if share_info.get("file_id"):
                    await _set_status(
                        analysis_id, frameio_asset_id=share_info["file_id"]
                    )
                video_path = os.path.join(workdir, "input.mp4")
                await _set_status(
                    analysis_id,
                    message="Downloading video from Frame.io share...",
                    progress=15,
                )
                ok = await frameio_service.download_video(
                    share_info["video_url"], video_path
                )
                if not ok:
                    await _set_status(
                        analysis_id,
                        status="failed",
                        error="Could not download the share video.",
                        progress=100,
                    )
                    return
            else:
                # ---- Owned asset via V2 API ----
                await _set_status(
                    analysis_id,
                    status="downloading",
                    progress=5,
                    message="Resolving Frame.io asset...",
                )
                asset_id = await frameio_service.resolve_asset_id(url)
                if not asset_id:
                    await _set_status(
                        analysis_id,
                        status="failed",
                        error="Could not extract Frame.io asset id from the URL. "
                        "Paste a direct player link, a share link, or upload "
                        "the video file.",
                        progress=100,
                    )
                    return
                await _set_status(analysis_id, frameio_asset_id=asset_id)

                asset = await frameio_service.get_asset(asset_id)
                if not asset:
                    await _set_status(
                        analysis_id,
                        status="failed",
                        error="Frame.io API rejected the request (403/404). "
                        "If this is a share link from another user, paste the "
                        "share URL directly (f.io/... or next.frame.io/share/...).",
                        progress=100,
                    )
                    return
                download_url = frameio_service.get_video_download_url(asset)
                if not download_url:
                    await _set_status(
                        analysis_id,
                        status="failed",
                        error="Asset has no downloadable video URL.",
                        progress=100,
                    )
                    return

                video_path = os.path.join(workdir, "input.mp4")
                await _set_status(
                    analysis_id,
                    message="Downloading video from Frame.io...",
                    progress=15,
                )
                ok = await frameio_service.download_video(download_url, video_path)
                if not ok:
                    await _set_status(
                        analysis_id,
                        status="failed",
                        error="Could not download the video.",
                        progress=100,
                    )
                    return

        duration = video_service.get_duration(video_path)
        await _set_status(
            analysis_id,
            status="extracting",
            duration_sec=duration,
            message="Extracting frames...",
            progress=25,
        )

        frames_dir = os.path.join(workdir, "frames")
        frames = await video_service.extract_frames(
            video_path, frames_dir, FRAME_INTERVAL
        )
        await _set_status(
            analysis_id,
            total_frames=len(frames),
            status="analyzing",
            message=f"Analyzing {len(frames)} frames with Gemini 3 Flash...",
            progress=30,
        )

        # analyze frames sequentially (free-tier safe) with progress updates
        all_issues: List[Issue] = []
        for idx, (ts, fpath) in enumerate(frames):
            errs = await ai_service.analyze_frame(fpath)
            for e in errs:
                issue = Issue(
                    timestamp_sec=float(ts),
                    type=e.get("type", "spelling"),
                    original=e.get("original", ""),
                    suggestion=e.get("suggestion", ""),
                    explanation=e.get("explanation", ""),
                    source_text=e.get("source_text", ""),
                    severity=_severity_for(e.get("type", "spelling")),
                )
                all_issues.append(issue)

            # progress: 30 → 80
            done = idx + 1
            prog = 30 + int(50 * done / max(len(frames), 1))
            await analyses_col.update_one(
                {"id": analysis_id},
                {
                    "$set": {
                        "analyzed_frames": done,
                        "progress": prog,
                        "issues": [i.model_dump() for i in all_issues],
                        "message": f"Analyzed frame {done}/{len(frames)} ({int(ts)}s)",
                    }
                },
            )

        # transcript pass (no timestamp)
        analysis = await _load(analysis_id)
        if analysis and analysis.transcript:
            await _set_status(
                analysis_id, message="Checking transcript...", progress=82
            )
            t_errors = await ai_service.analyze_transcript(analysis.transcript)
            for e in t_errors:
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

        # Auto-post to Frame.io
        analysis = await _load(analysis_id)
        posted = 0
        last_err: Optional[str] = None
        post_err_msg: Optional[str] = None
        if (
            analysis
            and analysis.auto_post
            and analysis.frameio_asset_id
            and all_issues
        ):
            await _set_status(
                analysis_id,
                status="posting",
                progress=85,
                message=f"Posting {len(all_issues)} comments to Frame.io...",
            )
            for issue in all_issues:
                text = _format_comment(issue)
                result = await frameio_service.post_comment(
                    analysis.frameio_asset_id,
                    text,
                    timestamp_seconds=issue.timestamp_sec if issue.timestamp_sec >= 0 else None,
                )
                if result.get("ok"):
                    issue.posted_to_frameio = True
                    cid = (result.get("comment") or {}).get("id")
                    issue.frameio_comment_id = cid
                    posted += 1
                else:
                    last_err = result.get("error")

        if (
            analysis
            and analysis.auto_post
            and analysis.frameio_asset_id
            and all_issues
            and posted == 0
        ):
            # All posts failed — likely a V4 share or token without write access
            short_err = (last_err or "").split(":", 1)[0].strip() or "unknown error"
            post_err_msg = (
                f"Auto-post to Frame.io failed ({short_err}). Public share "
                "links from other workspaces can't be commented on via a "
                "legacy developer token — paste a Frame.io URL from your own "
                "workspace to enable auto-posting."
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
                    "message": f"Done. {len(all_issues)} issues found, "
                    f"{posted} posted to Frame.io.",
                }
            },
        )
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        await _set_status(
            analysis_id,
            status="failed",
            error=str(exc),
            progress=100,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _format_comment(issue: Issue) -> str:
    label = {
        "spelling": "Spelling",
        "grammar": "Grammar",
        "punctuation": "Punctuation",
        "capitalization": "Capitalization",
    }.get(issue.type, "Issue")
    parts = [f"[{label}]"]
    if issue.original:
        parts.append(f'"{issue.original}"')
    if issue.suggestion:
        parts.append(f"→ \"{issue.suggestion}\"")
    if issue.explanation:
        parts.append(f"— {issue.explanation}")
    return " ".join(parts)


# ---------------- routes ----------------
@api.get("/")
async def root():
    return {"service": "Frame.io QA", "status": "ok"}


@api.get("/config")
async def config():
    return {
        "frameio_configured": bool(os.environ.get("FRAMEIO_TOKEN")),
        "llm_configured": bool(os.environ.get("EMERGENT_LLM_KEY")),
        "frame_interval": FRAME_INTERVAL,
    }


@api.post("/analyses")
async def create_analysis(
    background: BackgroundTasks,
    frameio_url: Optional[str] = Form(None),
    transcript: Optional[str] = Form(None),
    auto_post: bool = Form(True),
    video: Optional[UploadFile] = File(None),
):
    if not frameio_url and not video:
        raise HTTPException(
            status_code=400,
            detail="Provide either a Frame.io URL or a video file.",
        )

    analysis = Analysis(
        frameio_url=frameio_url,
        transcript=transcript,
        auto_post=auto_post,
        video_filename=video.filename if video else None,
    )
    await _save(analysis)

    video_local_path: Optional[str] = None
    if video:
        tmpdir = tempfile.mkdtemp(prefix=f"upload_{analysis.id}_")
        video_local_path = os.path.join(tmpdir, video.filename or "input.mp4")
        with open(video_local_path, "wb") as out:
            shutil.copyfileobj(video.file, out)

    background.add_task(_run_pipeline, analysis.id, video_local_path)
    return analysis.model_dump()


@api.get("/analyses")
async def list_analyses():
    cursor = analyses_col.find({}, {"_id": 0}).sort("created_at", -1).limit(50)
    items = await cursor.to_list(length=50)
    # trim issues from list view for payload size
    for it in items:
        it["issue_count"] = len(it.get("issues") or [])
        it["issues"] = []
    return items


@api.get("/analyses/{analysis_id}")
async def get_analysis(analysis_id: str):
    a = await _load(analysis_id)
    if not a:
        raise HTTPException(404, "Not found")
    return a.model_dump()


@api.post("/analyses/{analysis_id}/post")
async def manual_post(analysis_id: str):
    """Manually post any unposted issues to Frame.io."""
    a = await _load(analysis_id)
    if not a:
        raise HTTPException(404, "Not found")
    if not a.frameio_asset_id:
        raise HTTPException(400, "No Frame.io asset associated with this analysis.")

    posted = 0
    updated_issues: List[Issue] = []
    for issue in a.issues:
        if issue.posted_to_frameio:
            updated_issues.append(issue)
            continue
        text = _format_comment(issue)
        res = await frameio_service.post_comment(
            a.frameio_asset_id,
            text,
            timestamp_seconds=issue.timestamp_sec if issue.timestamp_sec >= 0 else None,
        )
        if res.get("ok"):
            issue.posted_to_frameio = True
            issue.frameio_comment_id = (res.get("comment") or {}).get("id")
            posted += 1
        updated_issues.append(issue)

    a.issues = updated_issues
    a.posted_count = sum(1 for i in a.issues if i.posted_to_frameio)
    await _save(a)
    return {"posted": posted, "total_posted": a.posted_count}


@api.delete("/analyses/{analysis_id}")
async def delete_analysis(analysis_id: str):
    res = await analyses_col.delete_one({"id": analysis_id})
    return {"deleted": res.deleted_count}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
