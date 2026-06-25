"""Iteration 7 — Adobe OAuth removed, guest-mode Playwright commenting.

Tests:
  * /api/config schema (only llm_configured, frame_interval, guest_name)
  * All /api/auth/adobe/* routes return 404
  * Password field plumbing — accepted on POST, stripped from GET responses
  * Regressions: history list, CSV export, manual post endpoint
  * Pydantic AnalyzeRequest still accepts password via Form
"""
from __future__ import annotations

import os
import time
import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL", "https://frame-spell-check.preview.emergentagent.com"
).rstrip("/")
API = f"{BASE_URL}/api"


# ---------- /api/config schema ----------
class TestConfig:
    def test_config_schema_only_guest_fields(self):
        r = requests.get(f"{API}/config", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        # Required keys
        assert "llm_configured" in data
        assert "frame_interval" in data
        assert "guest_name" in data
        assert data["guest_name"] == "Spellchecker"
        assert isinstance(data["llm_configured"], bool)
        assert isinstance(data["frame_interval"], (int, float))
        # Adobe-related keys MUST be absent
        for forbidden in ("adobe_connected", "adobe_user", "frameio_configured"):
            assert forbidden not in data, f"{forbidden} should be removed from /api/config"


# ---------- Adobe routes are gone ----------
class TestAdobeRoutesRemoved:
    def test_adobe_login_404(self):
        r = requests.get(f"{API}/auth/adobe/login", timeout=10, allow_redirects=False)
        assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text[:200]}"

    def test_adobe_callback_404(self):
        r = requests.get(
            f"{API}/auth/adobe/callback?code=fake&state=abc",
            timeout=10,
            allow_redirects=False,
        )
        assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text[:200]}"

    def test_adobe_logout_404(self):
        r = requests.post(f"{API}/auth/adobe/logout", timeout=10)
        assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text[:200]}"


# ---------- POST /api/analyses accepts password ----------
class TestPasswordPlumbing:
    @pytest.fixture(scope="class")
    def created_analysis(self):
        # Use a bogus URL that won't trigger expensive Playwright work right away
        # — we just want the model accepted with `password`
        r = requests.post(
            f"{API}/analyses",
            data={
                "frameio_url": "https://f.io/TEST_DUMMY_SHARE",
                "password": "TEST_secret_pw_123",
                "auto_post": "false",
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_pydantic_accepts_password_field(self, created_analysis):
        # Response from POST may still include `password` (it's the model dump
        # before strip), so just verify creation succeeded
        assert "id" in created_analysis
        assert created_analysis.get("frameio_url") == "https://f.io/TEST_DUMMY_SHARE"

    def test_get_analysis_strips_password(self, created_analysis):
        aid = created_analysis["id"]
        # Brief wait to let the pipeline fail (invalid share)
        time.sleep(2)
        r = requests.get(f"{API}/analyses/{aid}", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "password" not in d, f"GET /analyses/{{id}} leaked password: {d}"
        assert d["id"] == aid

    def test_list_analyses_strips_password_and_has_issue_count(self, created_analysis):
        r = requests.get(f"{API}/analyses", timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        for it in items:
            assert "password" not in it, "List leaked password"
            assert "issue_count" in it, "List missing issue_count"
            assert isinstance(it["issue_count"], int)
            # issues should be stripped to empty list in list view
            assert it.get("issues") == []

    def test_cleanup_dummy(self, created_analysis):
        aid = created_analysis["id"]
        r = requests.delete(f"{API}/analyses/{aid}", timeout=10)
        assert r.status_code == 200
        assert r.json().get("deleted") >= 0


# ---------- Validation ----------
class TestValidation:
    def test_create_analysis_requires_url_or_video(self):
        r = requests.post(f"{API}/analyses", data={"auto_post": "false"}, timeout=10)
        assert r.status_code == 400
        assert "frame.io" in r.text.lower() or "video" in r.text.lower()

    def test_get_unknown_analysis_404(self):
        r = requests.get(f"{API}/analyses/NONEXISTENT-ID-12345", timeout=10)
        assert r.status_code == 404

    def test_manual_post_requires_share_link(self):
        # Create an upload-style analysis (no frameio_url, fake file)
        files = {"video": ("tiny.mp4", b"\x00\x00\x00\x18ftypisom", "video/mp4")}
        r = requests.post(
            f"{API}/analyses",
            data={"auto_post": "false"},
            files=files,
            timeout=15,
        )
        assert r.status_code == 200
        aid = r.json()["id"]
        # Manual post should reject (no share link)
        r2 = requests.post(f"{API}/analyses/{aid}/post", timeout=10)
        assert r2.status_code == 400
        assert "share" in r2.text.lower()
        # Cleanup
        requests.delete(f"{API}/analyses/{aid}", timeout=10)


# ---------- CSV regression on most recent done analysis (if any) ----------
class TestCSV:
    def test_csv_export(self):
        r = requests.get(f"{API}/analyses", timeout=15)
        assert r.status_code == 200
        items = r.json()
        # Find any analysis
        if not items:
            pytest.skip("No analyses available for CSV regression")
        aid = items[0]["id"]
        r2 = requests.get(f"{API}/analyses/{aid}/csv", timeout=20)
        assert r2.status_code == 200
        assert "text/csv" in r2.headers.get("content-type", "").lower()
        # First row should be the header
        first_line = r2.text.splitlines()[0]
        assert "Timestamp" in first_line and "Severity" in first_line


# ---------- root ----------
class TestRoot:
    def test_root_ok(self):
        r = requests.get(f"{API}/", timeout=10)
        assert r.status_code == 200
        assert r.json().get("status") == "ok"
