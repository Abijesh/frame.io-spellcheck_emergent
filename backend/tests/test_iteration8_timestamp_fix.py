"""Iteration 8 — Frame.io comment timestamp BUG FIX verification.

BUG: Previously, all comments were anchored to frame ~14 on Frame.io because
setting `video.currentTime` via JS doesn't update Frame.io's player state.

FIX: Drive the player via keyboard shortcuts on [data-testid=playhead]:
  - Shift+ArrowRight ≈ 0.333s (coarse)
  - ArrowRight ≈ 1 frame (fine, 1/30s)
Comments sorted ASC so we only seek forward.

Evidence: analysis ca520f74-8b79-4225-aa2d-4d63510f69d7 (frameio_url=https://f.io/M6WUQCEn)
posted 8/8 with 4 DISTINCT timestamps: {0.0, 2.0, 6.0, 8.0} seconds.

Plus regressions: /api/config schema, Adobe routes still 404, CSV, manual /post.
"""
from __future__ import annotations

import os
import time

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
EVIDENCE_ANALYSIS_ID = "ca520f74-8b79-4225-aa2d-4d63510f69d7"


# ---------- BUG FIX evidence — distinct timestamps on existing analysis ----------
class TestTimestampBugFix:
    @pytest.fixture(scope="class")
    def evidence(self):
        r = requests.get(f"{API}/analyses/{EVIDENCE_ANALYSIS_ID}", timeout=15)
        if r.status_code != 200:
            pytest.skip(
                f"Evidence analysis {EVIDENCE_ANALYSIS_ID} not found — main agent ran new pipeline?"
            )
        return r.json()

    def test_evidence_analysis_done_and_posted_all(self, evidence):
        assert evidence["status"] == "done", f"status={evidence['status']}"
        n_issues = len(evidence.get("issues", []))
        assert n_issues == evidence["posted_count"], (
            f"len(issues)={n_issues} != posted_count={evidence['posted_count']}"
        )
        assert evidence["posted_count"] >= 8

    def test_distinct_timestamps_on_evidence(self, evidence):
        """The core proof: timestamps in the DB are spread (not all at ~14f / one value)."""
        issues = evidence["issues"]
        assert len(issues) >= 8
        ts = [round(float(i["timestamp_sec"]), 2) for i in issues]
        distinct = set(ts)
        # Pre-fix: only ONE distinct timestamp (the playhead default). Post-fix: ≥3.
        assert len(distinct) >= 3, (
            f"Only {len(distinct)} distinct timestamps in DB — bug not fixed. ts={ts}"
        )
        # Expected set (per main agent's manual visual verification on Frame.io):
        # {0.0, 2.0, 6.0, 8.0}
        expected = {0.0, 2.0, 6.0, 8.0}
        assert distinct.issuperset({0.0, 2.0, 6.0}), (
            f"Missing expected timestamps. Got {distinct}, expected superset of {expected}"
        )

    def test_all_issues_posted_to_frameio(self, evidence):
        for iss in evidence["issues"]:
            assert iss.get("posted_to_frameio") is True, (
                f"issue at t={iss.get('timestamp_sec')} not posted: {iss.get('post_error')}"
            )


# ---------- /api/config regression — no Adobe fields ----------
class TestConfigRegression:
    def test_config_has_only_guest_fields(self):
        r = requests.get(f"{API}/config", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d.get("guest_name") == "Proof.io"
        assert "llm_configured" in d
        assert "frame_interval" in d
        for forbidden in ("adobe_connected", "adobe_user", "frameio_configured"):
            assert forbidden not in d


# ---------- Adobe routes still 404 ----------
class TestAdobeStillGone:
    def test_adobe_login_404(self):
        r = requests.get(f"{API}/auth/adobe/login", timeout=10, allow_redirects=False)
        assert r.status_code == 404

    def test_adobe_callback_404(self):
        r = requests.get(
            f"{API}/auth/adobe/callback?code=fake&state=abc",
            timeout=10,
            allow_redirects=False,
        )
        assert r.status_code == 404

    def test_adobe_logout_404(self):
        r = requests.post(f"{API}/auth/adobe/logout", timeout=10)
        assert r.status_code == 404


# ---------- CSV regression on evidence analysis ----------
class TestCSV:
    def test_csv_export_evidence(self):
        r = requests.get(f"{API}/analyses/{EVIDENCE_ANALYSIS_ID}/csv", timeout=20)
        if r.status_code == 404:
            pytest.skip("Evidence analysis missing")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "").lower()
        lines = r.text.splitlines()
        assert "Timestamp" in lines[0] and "Severity" in lines[0]
        # 8 data rows + 1 header
        assert len(lines) >= 9, f"expected ≥9 CSV lines, got {len(lines)}"


# ---------- Manual /post regression — already-posted analysis returns 0 ----------
class TestManualPostIdempotent:
    def test_post_when_all_already_posted_returns_zero(self):
        r = requests.post(
            f"{API}/analyses/{EVIDENCE_ANALYSIS_ID}/post", timeout=30
        )
        if r.status_code == 404:
            pytest.skip("Evidence analysis missing")
        assert r.status_code == 200, r.text
        data = r.json()
        # Per main agent's spec: posted=0 (nothing new), total_posted=current (8)
        assert data.get("posted") == 0, f"expected posted=0, got {data}"
        assert data.get("total_posted", 0) >= 8, data


# ---------- root ----------
class TestRoot:
    def test_root_ok(self):
        r = requests.get(f"{API}/", timeout=10)
        assert r.status_code == 200
        assert r.json().get("status") == "ok"
