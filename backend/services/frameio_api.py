"""Official Frame.io V4 REST API client (as opposed to the Playwright-based
guest flow in frameio_service.py).

Sourced, not guessed:
  - base host api.frame.io/v4 and GET /v4/accounts: Quick Start guide
  - GET .../files/{file_id}?include=media_links.original -> download_url: forum
  - POST .../files/{file_id}/comments, body {"text","timestamp"}: migration guide
  - timestamp is a FRAME NUMBER starting at 1, not seconds: migration guide, verbatim

Every V4 request and response uses a JSON:API-style {"data": ...} envelope --
confirmed both live (GET) and against the official request-body docs (POST):
a list for collection endpoints (GET /v4/accounts), a single object for
resource endpoints (GET .../files/{id}, POST .../comments). Every parser
below unwraps "data" on the way out, and create_comment wraps it on the way in.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.frame.io/v4"


class FrameioApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


async def list_account_ids(access_token: str) -> List[str]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_BASE}/accounts", headers=_headers(access_token))
    if resp.status_code >= 400:
        raise FrameioApiError(resp.status_code, resp.text)
    data = resp.json()
    accounts = data.get("data", data) if isinstance(data, dict) else data
    return [a["id"] for a in accounts if "id" in a]


async def get_file_download_url(
    access_token: str, account_id: str, file_id: str
) -> Optional[str]:
    """Returns the official download URL for a file, or None if this identity
    doesn't have access (e.g. not a member of the file's project) -- callers
    should fall back to the anonymous scrape path in that case."""
    url = f"{API_BASE}/accounts/{account_id}/files/{file_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url,
            params={"include": "media_links.original"},
            headers=_headers(access_token),
        )
    if resp.status_code in (403, 404):
        logger.info("File %s not accessible to this Frame.io identity (%s)", file_id, resp.status_code)
        return None
    if resp.status_code >= 400:
        raise FrameioApiError(resp.status_code, resp.text)
    body = resp.json()
    data = body.get("data", body) if isinstance(body, dict) else body
    url = (data.get("media_links") or {}).get("original", {}).get("download_url")
    if not url:
        logger.info("File %s has no media_links.original.download_url yet (status=%s)", file_id, data.get("status"))
    return url


async def create_comment(
    access_token: str, account_id: str, file_id: str, text: str, frame: int
) -> dict:
    url = f"{API_BASE}/accounts/{account_id}/files/{file_id}/comments"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            json={"data": {"text": text, "timestamp": frame}},
            headers=_headers(access_token),
        )
    if resp.status_code >= 400:
        raise FrameioApiError(resp.status_code, resp.text)
    body = resp.json()
    return body.get("data", body) if isinstance(body, dict) else body


def sec_to_frame(timestamp_sec: float, fps: float) -> int:
    """V4 comment timestamps are frame numbers starting at 1, not seconds."""
    if fps <= 0:
        fps = 24.0  # matches video_service.get_fps's own fallback
    return max(1, round(timestamp_sec * fps) + 1)
