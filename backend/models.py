"""Pydantic models for the Frame.io QA service."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Issue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_sec: float = 0.0
    end_sec: Optional[float] = None  # last frame this on-screen text was seen at
    type: str = "spelling"  # spelling | grammar | punctuation | capitalization
    original: str = ""
    suggestion: str = ""
    explanation: str = ""
    source_text: str = ""
    severity: str = "medium"  # low | medium | high
    thumbnail_b64: Optional[str] = None  # JPEG crop of the flagged text, base64
    posted_to_frameio: bool = False
    frameio_comment_id: Optional[str] = None
    posted_via: Optional[str] = None  # "official_api" | "guest"


class Analysis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    frameio_url: Optional[str] = None
    frameio_asset_id: Optional[str] = None
    video_filename: Optional[str] = None
    transcript: Optional[str] = None
    password: Optional[str] = None
    auto_post: bool = True
    check_contrast: bool = False
    allowlist: Optional[str] = None  # raw comma/newline-separated terms
    password_required: bool = False

    status: str = "queued"  # queued | downloading | extracting | analyzing | posting | done | failed
    progress: int = 0  # 0..100
    message: str = ""

    total_frames: int = 0
    analyzed_frames: int = 0
    duration_sec: float = 0.0
    video_fps: float = 0.0

    issues: List[Issue] = Field(default_factory=list)
    posted_count: int = 0
    post_error: Optional[str] = None
    error: Optional[str] = None
    deleted: bool = False
    deleted_at: Optional[str] = None
    # Set when Gemini's quota ran out partway through -- issues found before
    # that point are real and kept; unchecked_instances counts how many
    # on-screen text instances were never actually checked as a result (not
    # the same as "checked and found clean").
    quota_exceeded: bool = False
    unchecked_instances: int = 0


class AnalyzeRequest(BaseModel):
    frameio_url: Optional[str] = None
    transcript: Optional[str] = None
    auto_post: bool = True


class FrameioSession(BaseModel):
    """A connected Frame.io account, keyed by an opaque id stored in the
    user's session cookie -- the actual OAuth tokens never reach the browser."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    access_token: str
    refresh_token: str
    expires_at: str  # ISO timestamp
    # A single Adobe identity can belong to several Frame.io accounts (a
    # personal account plus one or more team workspaces) with no "default"
    # indicated anywhere in the API -- so we keep all of them and try each
    # one when resolving whether a given file is reachable, instead of
    # guessing the first one is the right one.
    account_ids: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now_iso)
