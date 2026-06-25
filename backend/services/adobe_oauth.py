"""Adobe IMS OAuth + Frame.io V4 API helpers.

Single-user model: we keep one OAuth session (`user_id="default"`) since the
app has no public auth. Whoever connects last is the active Frame.io user.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

ADOBE_AUTH_URL = "https://ims-na1.adobelogin.com/ims/authorize/v2"
ADOBE_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FRAMEIO_V4 = "https://api.frame.io/v4"

USER_KEY = "default"


def _client_id() -> str:
    return os.environ.get("ADOBE_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("ADOBE_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return os.environ.get("ADOBE_REDIRECT_URI", "")


def _scopes() -> str:
    return os.environ.get(
        "ADOBE_SCOPES", "openid,email,profile,offline_access,additional_info.roles"
    )


def _fernet() -> Fernet:
    key = os.environ["TOKEN_ENCRYPTION_KEY"]
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(v: str) -> str:
    return _fernet().encrypt(v.encode()).decode()


def decrypt(v: str) -> str:
    return _fernet().decrypt(v.encode()).decode()


def _basic_auth() -> str:
    raw = f"{_client_id()}:{_client_secret()}".encode()
    return base64.b64encode(raw).decode()


def new_state() -> str:
    """Return a Fernet-signed nonce. Stateless: no DB lookup needed on
    callback, so the flow survives backend restarts and works across
    multiple browser tabs."""
    nonce = secrets.token_urlsafe(16)
    return _fernet().encrypt(nonce.encode()).decode()


def verify_state(state: str) -> bool:
    if not state:
        return False
    try:
        # Accept tokens up to 30 minutes old
        _fernet().decrypt(state.encode(), ttl=1800)
        return True
    except Exception:
        return False


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": _scopes(),
        "response_type": "code",
        "state": state,
    }
    return f"{ADOBE_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            ADOBE_TOKEN_URL,
            headers={
                "Authorization": f"Basic {_basic_auth()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"code": code, "grant_type": "authorization_code"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Adobe token exchange {r.status_code}: {r.text[:300]}")
    return r.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            ADOBE_TOKEN_URL,
            headers={
                "Authorization": f"Basic {_basic_auth()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Adobe refresh {r.status_code}: {r.text[:300]}")
    return r.json()


async def save_tokens(db, token_data: dict) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_data.get("expires_in", 3600))
    )
    await db.adobe_tokens.replace_one(
        {"user_id": USER_KEY},
        {
            "user_id": USER_KEY,
            "access_token": encrypt(token_data["access_token"]),
            "refresh_token": encrypt(token_data["refresh_token"]),
            "expires_at": expires_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        upsert=True,
    )


async def get_valid_access_token(db) -> Optional[str]:
    doc = await db.adobe_tokens.find_one({"user_id": USER_KEY})
    if not doc:
        return None
    try:
        expires_at = datetime.fromisoformat(doc["expires_at"])
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    if expires_at > now + timedelta(seconds=60):
        return decrypt(doc["access_token"])
    # refresh
    try:
        token_data = await refresh_access_token(decrypt(doc["refresh_token"]))
    except Exception as exc:
        logger.warning("Adobe token refresh failed: %s", exc)
        return None
    # Adobe may not return a new refresh_token — fall back to the old one
    token_data.setdefault("refresh_token", decrypt(doc["refresh_token"]))
    await save_tokens(db, token_data)
    return token_data["access_token"]


async def clear_tokens(db) -> None:
    await db.adobe_tokens.delete_many({"user_id": USER_KEY})


# -------------------- Frame.io V4 API helpers --------------------
async def v4_get(token: str, path: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{FRAMEIO_V4}{path}",
            headers={"Authorization": f"Bearer {token}", "api-version": "experimental"},
        )
    if r.status_code >= 400:
        logger.warning("V4 GET %s -> %s %s", path, r.status_code, r.text[:200])
        return None
    return r.json()


async def get_me(token: str) -> Optional[dict]:
    """V4 /me returns {data: {id, name, email}} — no api-version header here."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{FRAMEIO_V4}/me",
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code >= 400:
        logger.warning("V4 /me -> %s %s", r.status_code, r.text[:200])
        return None
    return r.json()


async def get_account_id(token: str) -> Optional[str]:
    """List accounts the user can access and return the first account id."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{FRAMEIO_V4}/accounts",
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code >= 400:
        logger.warning("V4 /accounts -> %s %s", r.status_code, r.text[:200])
        return None
    data = r.json().get("data") or []
    return data[0]["id"] if data else None


async def get_share_files(
    token: str, account_id: str, share_id: str
) -> Optional[list]:
    data = await v4_get(
        token, f"/accounts/{account_id}/shares/{share_id}/files"
    )
    if not data:
        return None
    # Frame.io V4 returns {"data": [...]} or list
    return data.get("data") if isinstance(data, dict) else data


async def get_file(token: str, account_id: str, file_id: str) -> Optional[dict]:
    data = await v4_get(token, f"/accounts/{account_id}/files/{file_id}")
    if not data:
        return None
    return data.get("data") if isinstance(data, dict) and "data" in data else data


def pick_download_url(file_obj: dict) -> Optional[str]:
    """Pull a downloadable mp4/m4v URL out of a V4 file object."""
    if not file_obj:
        return None
    for k in ("media_links", "download_links", "downloads"):
        v = file_obj.get(k)
        if isinstance(v, dict):
            for url in v.values():
                if isinstance(url, str) and url.startswith("http"):
                    return url
    for k in ("original_url", "h264_540", "h264_720", "h264_1080", "original"):
        v = file_obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


async def post_comment_v4(
    token: str,
    account_id: str,
    file_id: str,
    text: str,
    timestamp_frames: Optional[int] = None,
) -> dict:
    """Post a comment to a V4 file. `timestamp` is in FRAMES (integer)."""
    data: dict = {"text": text}
    if timestamp_frames is not None and timestamp_frames >= 0:
        data["timestamp"] = int(timestamp_frames)
    payload = {"data": data}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{FRAMEIO_V4}/accounts/{account_id}/files/{file_id}/comments",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code in (200, 201):
            return {"ok": True, "comment": r.json()}
        return {"ok": False, "error": f"{r.status_code}: {r.text[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
