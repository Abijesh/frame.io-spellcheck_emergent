"""Frame.io QA backend (guest-mode commenting).

No Adobe OAuth. Pipeline per analysis:
  1. If a Frame.io share URL is given, open it in headless Chromium
     (fills password if provided) and grab the signed video CDN URL.
     If a video was uploaded, use that directly and skip step 5.
  2. ffmpeg extracts one frame every FRAME_SAMPLE_INTERVAL seconds.
  3. Gemini 3 Flash analyses each frame for OCR + spelling/grammar.
  4. Optional transcript-level pass.
  5. Headless Chromium opens the share again, posts each issue as a guest
     comment under the name 'Spellchecker' at the right timestamp.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
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


def _severity_for(err_type: str) -> str:
    return {
        "spelling": "high",
        "grammar": "high",
        "punctuation": "low",
        "capitalization": "medium",
    }.get(err_type, "medium")


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
            ok = await frameio_service.download_video(info["video_url"], video_path)
            if not ok:
                await _set_status(
                    analysis_id,
                    status="failed",
                    error="Failed to download the video.",
                    progress=100,
                )
                return

        duration = video_service.get_duration(video_path)
        fps = video_service.get_fps(video_path)
        await _set_status(
            analysis_id,
            status="extracting",
            duration_sec=duration,
            video_fps=fps,
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

        all_issues: List[Issue] = []
        completed = 0
        sem = asyncio.Semaphore(3)  # cap concurrent Gemini calls

        async def _analyze_one(ts: float, fpath: str):
            async with sem:
                errs = await ai_service.analyze_frame(fpath)
            return ts, errs

        async def _run_with_progress():
            nonlocal completed
            tasks = [
                asyncio.create_task(_analyze_one(ts, fp)) for ts, fp in frames
            ]
            for fut in asyncio.as_completed(tasks):
                ts, errs = await fut
                for e in errs:
                    all_issues.append(
                        Issue(
                            timestamp_sec=float(ts),
                            type=e.get("type", "spelling"),
                            original=e.get("original", ""),
                            suggestion=e.get("suggestion", ""),
                            explanation=e.get("explanation", ""),
                            source_text=e.get("source_text", ""),
                            severity=_severity_for(e.get("type", "spelling")),
                        )
                    )
                completed += 1
                await analyses_col.update_one(
                    {"id": analysis_id},
                    {
                        "$set": {
                            "analyzed_frames": completed,
                            "progress": 30 + int(50 * completed / max(len(frames), 1)),
                            "issues": [i.model_dump() for i in all_issues],
                            "message": f"Analyzed {completed}/{len(frames)} frames",
                        }
                    },
                )

        await _run_with_progress()

        # transcript pass (no timestamp)
        analysis = await _load(analysis_id)
        if analysis and analysis.transcript:
            await _set_status(
                analysis_id, message="Checking transcript...", progress=82
            )
            for e in await ai_service.analyze_transcript(analysis.transcript):
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

        # Guest comment posting
        analysis = await _load(analysis_id)
        posted = 0
        post_err_msg: Optional[str] = None
        if (
            analysis
            and analysis.auto_post
            and analysis.frameio_url
            and frameio_service.is_share_link(analysis.frameio_url)
            and all_issues
        ):
            await _set_status(
                analysis_id,
                status="posting",
                progress=85,
                message=f"Posting {len(all_issues)} comments as Spellchecker...",
            )
            payloads = [
                {
                    "timestamp_sec": i.timestamp_sec,
                    "text": _format_comment(i),
                }
                for i in all_issues
            ]
            result = await frameio_service.submit_guest_comments(
                analysis.frameio_url, analysis.password, payloads
            )
            flags = result.get("posted_flags") or []
            for idx, issue in enumerate(all_issues):
                if idx < len(flags) and flags[idx]:
                    issue.posted_to_frameio = True
            posted = result.get("posted", 0)
            if result.get("error"):
                post_err_msg = result["error"]
            elif result.get("failed", 0) > 0 and posted == 0:
                post_err_msg = (
                    "Frame.io rejected all comments. The share owner may have "
                    "disabled commenting on this link."
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
                    f"{posted} posted as Spellchecker.",
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
        "llm_configured": bool(os.environ.get("EMERGENT_LLM_KEY")),
        "frame_interval": FRAME_INTERVAL,
        "guest_name": frameio_service.GUEST_NAME,
    }


@api.post("/analyses")
async def create_analysis(
    background: BackgroundTasks,
    frameio_url: Optional[str] = Form(None),
    transcript: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    auto_post: bool = Form(True),
    video: Optional[UploadFile] = File(None),
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
    d = analysis.model_dump()
    d.pop("password", None)
    return d


@api.get("/analyses")
async def list_analyses():
    cursor = analyses_col.find({}, {"_id": 0}).sort("created_at", -1).limit(50)
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
async def post_single_issue(analysis_id: str, issue_id: str):
    """Post one specific issue to Frame.io as a guest comment."""
    a = await _load(analysis_id)
    if not a:
        raise HTTPException(404, "Not found")
    if not a.frameio_url or not frameio_service.is_share_link(a.frameio_url):
        raise HTTPException(
            400, "Only Frame.io share links support guest comment posting."
        )
    idx = next(
        (i for i, x in enumerate(a.issues) if x.id == issue_id), None
    )
    if idx is None:
        raise HTTPException(404, "Issue not found")
    issue = a.issues[idx]
    if issue.posted_to_frameio:
        return {"posted": False, "already": True}

    result = await frameio_service.submit_guest_comments(
        a.frameio_url,
        a.password,
        [{"timestamp_sec": issue.timestamp_sec, "text": _format_comment(issue)}],
    )
    flags = result.get("posted_flags") or [False]
    if flags[0]:
        a.issues[idx].posted_to_frameio = True
        a.posted_count = sum(1 for i in a.issues if i.posted_to_frameio)
        await _save(a)
        return {"posted": True}
    return {
        "posted": False,
        "error": result.get("error") or "Frame.io rejected the comment.",
    }


@api.post("/analyses/{analysis_id}/post")
async def manual_post(analysis_id: str):
    a = await _load(analysis_id)
    if not a:
        raise HTTPException(404, "Not found")
    if not a.frameio_url or not frameio_service.is_share_link(a.frameio_url):
        raise HTTPException(
            400, "Only Frame.io share links support guest comment posting."
        )

    unposted_idx = [i for i, x in enumerate(a.issues) if not x.posted_to_frameio]
    if not unposted_idx:
        return {"posted": 0, "total_posted": a.posted_count}

    payloads = [
        {
            "timestamp_sec": a.issues[i].timestamp_sec,
            "text": _format_comment(a.issues[i]),
        }
        for i in unposted_idx
    ]
    result = await frameio_service.submit_guest_comments(
        a.frameio_url, a.password, payloads
    )
    flags = result.get("posted_flags") or []
    for j, idx in enumerate(unposted_idx):
        if j < len(flags) and flags[j]:
            a.issues[idx].posted_to_frameio = True
    a.posted_count = sum(1 for i in a.issues if i.posted_to_frameio)
    await _save(a)
    return {
        "posted": result.get("posted", 0),
        "total_posted": a.posted_count,
        "error": result.get("error"),
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
        w.writerow([
            mm_ss,
            "" if ts < 0 else f"{ts:.2f}",
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
