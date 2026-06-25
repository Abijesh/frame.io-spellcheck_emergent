"""Iteration 5 - OAuth State fix regression tests.

Tests:
1. /api/auth/adobe/login returns 307 redirect with Fernet-signed state param
2. /api/auth/adobe/callback with valid state proceeds past state-check
   (fails at code exchange because code is fake) -> redirects to FRONTEND_URL/?adobe_error=...
3. /api/auth/adobe/callback with garbage state -> 400 Invalid OAuth state
4. /api/auth/adobe/callback with empty state -> 4xx (FastAPI 422 for missing param OK)
5. Regression: /api/config still shows adobe_connected:true + correct email
6. Regression: CSV endpoint works for an existing analysis
7. Confirm no new docs added to oauth_state collection on /login
"""
import os
import re
import urllib.parse

import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://frame-spell-check.preview.emergentagent.com").rstrip("/")
EXPECTED_EMAIL = "abijeshgreg@gmail.com"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def db():
    # Read backend env directly
    env = {}
    with open("/app/backend/.env") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    client = MongoClient(env["MONGO_URL"])
    return client[env["DB_NAME"]]


# ---------- Bug fix tests ----------

def test_adobe_login_returns_redirect_with_fernet_state(session):
    """GET /api/auth/adobe/login -> 307 redirect to Adobe IMS authorize with Fernet state."""
    r = session.get(f"{BASE_URL}/api/auth/adobe/login", allow_redirects=False)
    assert r.status_code in (302, 307), f"expected redirect, got {r.status_code}: {r.text[:200]}"
    loc = r.headers.get("location") or r.headers.get("Location")
    assert loc, "no Location header"
    assert loc.startswith("https://ims-na1.adobelogin.com/ims/authorize/v2"), f"bad redirect target: {loc}"

    parsed = urllib.parse.urlparse(loc)
    qs = urllib.parse.parse_qs(parsed.query)
    state = qs.get("state", [None])[0]
    assert state, "state param missing from authorize url"
    # Fernet tokens are URL-safe base64 of header(9)+iv(16)+ciphertext+hmac(32);
    # 16-byte nonce encrypted -> ~100+ chars
    assert len(state) >= 100, f"state too short ({len(state)} chars) — looks like old random token, not Fernet"
    # Fernet tokens start with 'gAAAAA' (version 0x80 base64-prefix)
    assert state.startswith("gAAAAA"), f"state does not look like a Fernet token: {state[:20]}"


def test_adobe_callback_with_valid_state_proceeds_past_state_check(session):
    """A valid signed state must NOT trigger 'Invalid OAuth state' — it should fail later at code exchange."""
    # First, grab a fresh signed state
    r1 = session.get(f"{BASE_URL}/api/auth/adobe/login", allow_redirects=False)
    loc = r1.headers["location"]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    valid_state = qs["state"][0]

    # Now call callback with that state + a fake code
    r2 = session.get(
        f"{BASE_URL}/api/auth/adobe/callback",
        params={"code": "FAKE_CODE_FOR_TEST", "state": valid_state},
        allow_redirects=False,
    )
    # The state check should pass. Code exchange will fail -> redirect to FRONTEND_URL/?adobe_error=...
    # Acceptable outcomes:
    #   - 307/302 redirect to /?adobe_error=
    # NOT acceptable:
    #   - 400 with "Invalid OAuth state"
    if r2.status_code in (302, 307):
        target = r2.headers.get("location", "")
        assert "adobe_error" in target, f"expected adobe_error in redirect, got: {target}"
        assert "Invalid OAuth state" not in target
    else:
        # Some implementations return 200 with HTML — fail explicitly if it's the bug
        body = r2.text
        assert "Invalid OAuth state" not in body, f"State check rejected a valid signed state. Body: {body[:300]}"


def test_adobe_callback_with_garbage_state_returns_400(session):
    r = session.get(
        f"{BASE_URL}/api/auth/adobe/callback",
        params={"code": "FAKE_CODE", "state": "this-is-not-a-fernet-token"},
        allow_redirects=False,
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"
    assert "Invalid OAuth state" in r.text


def test_adobe_callback_with_empty_state_returns_4xx(session):
    # FastAPI may reject missing-or-empty query param with 422; explicit empty value -> our 400 path
    r = session.get(
        f"{BASE_URL}/api/auth/adobe/callback",
        params={"code": "FAKE_CODE", "state": ""},
        allow_redirects=False,
    )
    assert 400 <= r.status_code < 500, f"expected 4xx, got {r.status_code}: {r.text[:200]}"


# ---------- Regression tests ----------

def test_config_still_shows_adobe_connected(session):
    r = session.get(f"{BASE_URL}/api/config")
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert data.get("adobe_connected") is True, f"adobe_connected lost! {data}"
    adobe_user = data.get("adobe_user") or {}
    assert adobe_user.get("email") == EXPECTED_EMAIL, f"adobe_user.email mismatch: {adobe_user}"


def test_csv_endpoint_still_works(session):
    """Find an existing done analysis and download its CSV."""
    r = session.get(f"{BASE_URL}/api/analyses")
    assert r.status_code == 200
    analyses = r.json()
    done_ones = [a for a in analyses if a.get("status") == "done" and (a.get("posted_count") or 0) > 0]
    if not done_ones:
        pytest.skip("No done analyses to regression-test CSV")
    aid = done_ones[0]["id"]
    csv_r = session.get(f"{BASE_URL}/api/analyses/{aid}/csv")
    assert csv_r.status_code == 200, csv_r.text[:200]
    ctype = csv_r.headers.get("content-type", "")
    assert "csv" in ctype.lower(), f"bad content-type: {ctype}"
    # Header row
    first_line = csv_r.text.splitlines()[0]
    assert "Timestamp" in first_line, f"CSV header missing Timestamp: {first_line}"


# ---------- DB side effect check ----------

def test_login_does_not_write_to_oauth_state_collection(db, session):
    before = db.oauth_state.count_documents({})
    # Hit login 3 times
    for _ in range(3):
        session.get(f"{BASE_URL}/api/auth/adobe/login", allow_redirects=False)
    after = db.oauth_state.count_documents({})
    assert after == before, f"oauth_state collection grew from {before} to {after} — stateless flow not in effect"
