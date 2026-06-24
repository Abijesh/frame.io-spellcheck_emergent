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
    type: str = "spelling"  # spelling | grammar | punctuation | capitalization
    original: str = ""
    suggestion: str = ""
    explanation: str = ""
    source_text: str = ""
    severity: str = "medium"  # low | medium | high
    posted_to_frameio: bool = False
    frameio_comment_id: Optional[str] = None


class Analysis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    frameio_url: Optional[str] = None
    frameio_asset_id: Optional[str] = None
    video_filename: Optional[str] = None
    transcript: Optional[str] = None
    auto_post: bool = True

    status: str = "queued"  # queued | downloading | extracting | analyzing | posting | done | failed
    progress: int = 0  # 0..100
    message: str = ""

    total_frames: int = 0
    analyzed_frames: int = 0
    duration_sec: float = 0.0

    issues: List[Issue] = Field(default_factory=list)
    posted_count: int = 0
    error: Optional[str] = None


class AnalyzeRequest(BaseModel):
    frameio_url: Optional[str] = None
    transcript: Optional[str] = None
    auto_post: bool = True
