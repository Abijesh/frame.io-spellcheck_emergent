"""Adobe IMS OAuth for the Frame.io V4 API (OAuth Web App flow).

Endpoints and scopes below are sourced from Frame.io's own docs/forum and the
user's actual Adobe Developer Console project, not guessed:
  - authorize/token hosts: next.developer.frame.io auth docs + forum threads
  - scopes: exactly what's configured on the project's OAuth Web App credential
  - api.frame.io/v4 base + GET /v4/accounts to discover account_id: Quick Start guide
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

import httpx

IMS_AUTHORIZE_URL = "https://ims-na1.adobelogin.com/ims/authorize/v2"
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"

# Exactly the scopes shown on this project's Adobe Developer Console credential.
SCOPES = "openid profile email additional_info.roles offline_access"


def _client_id() -> str:
    return os.environ.get("FRAMEIO_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("FRAMEIO_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return os.environ.get(
        "FRAMEIO_REDIRECT_URI", "https://localhost:8000/api/frameio/oauth/callback"
    )


def is_configured() -> bool:
    return bool(_client_id() and _client_secret())


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": SCOPES,
        "response_type": "code",
        "state": state,
    }
    return f"{IMS_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Trade an authorization code for access_token/refresh_token/expires_in."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            IMS_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri": _redirect_uri(),
                "code": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    resp.raise_for_status()
    return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            IMS_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    resp.raise_for_status()
    return resp.json()
