"""Iteration 6 — Verify the relaxed verify_state() now accepts BOTH:
- a valid Fernet-signed token (new flow), AND
- a 22..64 char URL-safe legacy nonce (cached pre-Fernet Adobe URLs).

Regressions:
- garbage / empty / illegal-char state still rejected with 4xx.
- /api/auth/adobe/login still 307 -> Adobe IMS with a Fernet state.
- /api/config still 200 with `adobe_connected` boolean.
- CSV endpoint still text/csv for an existing analysis.
"""
import os
import re
import urllib.parse

import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL", "https://frame-spell-check.preview.emergentagent.com"
).rstrip("/")

LEGACY_STATE = "RWlDXQR8cIixyKCBJSSFq2M4Dj1haDOc"  # 32 chars, URL-safe, from user's bug report


# ---------- helpers ----------
def _callback(state: str, code: str = "fake"):
    """Hit /api/auth/adobe/callback without following the redirect."""
    return requests.get(
        f"{BASE_URL}/api/auth/adobe/callback",
        params={"code": code, "state": state},
        allow_redirects=False,
        timeout=30,
    )


def _login_state() -> str:
    """Hit /api/auth/adobe/login, capture the Fernet state from the redirect."""
    r = requests.get(
        f"{BASE_URL}/api/auth/adobe/login", allow_redirects=False, timeout=15
    )
    assert r.status_code in (302, 303, 307), f"unexpected login status {r.status_code}"
    loc = r.headers["location"]
    qs = urllib.parse.urlparse(loc).query
    state = urllib.parse.parse_qs(qs).get("state", [None])[0]
    assert state, f"no state in login redirect: {loc}"
    return state


# ---------- BUG FIX assertions ----------
class TestBugFix:
    """verify_state must now accept legacy + Fernet states."""

    def test_legacy_state_passes_state_check(self):
        """The exact state from the user's bug report (32-char URL-safe).
        Expect a redirect (302) to FRONTEND_URL/?adobe_error=... — NOT 400."""
        r = _callback(LEGACY_STATE)
        assert r.status_code != 400, (
            f"Legacy state was REJECTED. body={r.text[:300]}"
        )
        assert r.status_code in (302, 303, 307), (
            f"Expected redirect, got {r.status_code}: {r.text[:300]}"
        )
        loc = r.headers.get("location", "")
        assert "adobe_error" in loc, (
            f"Expected adobe_error in redirect location, got: {loc}"
        )

    def test_fresh_fernet_state_passes_state_check(self):
        state = _login_state()
        # Fernet token always starts with 'gAAAAA' (base64 of magic byte 0x80)
        assert state.startswith("gAAAAA"), f"not a Fernet token: {state[:20]}"
        r = _callback(state)
        assert r.status_code != 400, (
            f"Fernet state was REJECTED. body={r.text[:300]}"
        )
        assert r.status_code in (302, 303, 307), (
            f"Expected redirect, got {r.status_code}"
        )
        loc = r.headers.get("location", "")
        assert "adobe_error" in loc, (
            f"Expected adobe_error in redirect location, got: {loc}"
        )


# ---------- REGRESSION assertions ----------
class TestRegression:
    def test_short_state_rejected(self):
        r = _callback("hi")
        assert r.status_code == 400, f"Expected 400 for short state, got {r.status_code}"
        assert "Invalid OAuth state" in r.text

    def test_illegal_chars_state_rejected(self):
        r = _callback("<script>alert(1)</script>")
        assert r.status_code == 400, (
            f"Expected 400 for illegal-char state, got {r.status_code}: {r.text[:200]}"
        )
        assert "Invalid OAuth state" in r.text

    def test_empty_state_rejected(self):
        # Empty string OR missing param — both should be 4xx (FastAPI may return
        # 422 for missing required query param, or 400 from verify_state for empty).
        r = _callback("")
        assert 400 <= r.status_code < 500, (
            f"Expected 4xx for empty state, got {r.status_code}: {r.text[:200]}"
        )

    def test_login_returns_307_to_adobe_with_fernet_state(self):
        r = requests.get(
            f"{BASE_URL}/api/auth/adobe/login", allow_redirects=False, timeout=15
        )
        assert r.status_code == 307, f"Expected 307, got {r.status_code}"
        loc = r.headers["location"]
        assert "ims-na1.adobelogin.com/ims/authorize/v2" in loc, loc
        # Confirm state= is a Fernet token (starts with gAAAAA url-encoded as gAAAAA)
        m = re.search(r"[?&]state=([^&]+)", loc)
        assert m, f"no state= in {loc}"
        state = urllib.parse.unquote(m.group(1))
        assert state.startswith("gAAAAA"), f"state is not Fernet: {state[:30]}"
        assert 100 <= len(state) <= 200, f"unexpected state length {len(state)}"

    def test_config_endpoint(self):
        r = requests.get(f"{BASE_URL}/api/config", timeout=15)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
        data = r.json()
        assert "adobe_connected" in data, data
        assert isinstance(data["adobe_connected"], bool), type(data["adobe_connected"])

    def test_csv_endpoint_returns_text_csv(self):
        # Find an existing analysis
        r = requests.get(f"{BASE_URL}/api/analyses", timeout=15)
        assert r.status_code == 200
        analyses = r.json()
        if not analyses:
            pytest.skip("no analyses in DB to regression-test CSV")
        analysis_id = analyses[0]["id"]
        r = requests.get(
            f"{BASE_URL}/api/analyses/{analysis_id}/csv", timeout=20
        )
        assert r.status_code == 200, f"csv got {r.status_code}: {r.text[:200]}"
        ctype = r.headers.get("content-type", "")
        assert "text/csv" in ctype, f"unexpected content-type: {ctype}"
