"""Adobe IMS OAuth route tests (iteration 3)."""
import os
import urllib.parse
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL", "https://frame-spell-check.preview.emergentagent.com"
).rstrip("/")
API = f"{BASE_URL}/api"


# /api/config exposes adobe_connected + adobe_user
def test_config_includes_adobe_fields():
    r = requests.get(f"{API}/config", timeout=30)
    assert r.status_code == 200
    data = r.json()
    assert "adobe_connected" in data
    assert "adobe_user" in data
    # When nobody signed in:
    if data["adobe_connected"] is False:
        assert data["adobe_user"] is None


# /api/auth/adobe/login → 307 to Adobe IMS w/ correct params
def test_adobe_login_redirects_to_ims():
    r = requests.get(f"{API}/auth/adobe/login", allow_redirects=False, timeout=30)
    assert r.status_code in (302, 303, 307)
    loc = r.headers.get("location", "")
    assert loc.startswith("https://ims-na1.adobelogin.com/ims/authorize/v2"), loc
    parsed = urllib.parse.urlparse(loc)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs.get("response_type") == ["code"]
    assert qs.get("client_id") and qs["client_id"][0]
    assert qs.get("redirect_uri") and "/api/auth/adobe/callback" in qs["redirect_uri"][0]
    assert qs.get("scope") and qs["scope"][0]
    assert qs.get("state") and len(qs["state"][0]) > 10


# /api/auth/adobe/callback with invalid state → 400
def test_adobe_callback_invalid_state_returns_400():
    r = requests.get(
        f"{API}/auth/adobe/callback",
        params={"code": "fake", "state": "definitely-not-a-real-state-xyz"},
        allow_redirects=False,
        timeout=30,
    )
    assert r.status_code == 400
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    detail = (body.get("detail") or "").lower()
    assert "invalid" in detail and "state" in detail


# /api/auth/adobe/logout returns {ok:true} and is idempotent
def test_adobe_logout_idempotent():
    for _ in range(2):
        r = requests.post(f"{API}/auth/adobe/logout", timeout=30)
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# After logout, config reports disconnected
def test_config_disconnected_after_logout():
    requests.post(f"{API}/auth/adobe/logout", timeout=30)
    r = requests.get(f"{API}/config", timeout=30)
    assert r.status_code == 200
    data = r.json()
    assert data["adobe_connected"] is False
    assert data["adobe_user"] is None
