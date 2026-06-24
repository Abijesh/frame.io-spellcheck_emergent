"""Iteration 4 tests:

1) BUG FIX: share-link analysis with Adobe-connected account should reach
   status=done, video_fps>0, posted_count == len(issues), post_error is None.
2) NEW: GET /api/analyses/{id}/csv returns text/csv attachment with the
   expected header row and one row per issue.
3) Regression: /api/config exposes adobe_user, invalid URL fails gracefully,
   uploaded video pipeline reaches status=done.
"""
import os
import time
import io
import csv as csv_mod

import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://frame-spell-check.preview.emergentagent.com",
).rstrip("/")
API = f"{BASE_URL}/api"
SHARE_URL = "https://f.io/M6WUQCEn"
TEST_VIDEO = "/tmp/test.mp4"


def _poll(aid, max_wait=180):
    deadline = time.time() + max_wait
    last = None
    while time.time() < deadline:
        r = requests.get(f"{API}/analyses/{aid}", timeout=30)
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("done", "failed"):
            return last
        time.sleep(3)
    return last


# ---------- config ----------
def test_config_adobe_connected():
    r = requests.get(f"{API}/config", timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert d["frameio_configured"] is True
    assert d["llm_configured"] is True
    assert d["adobe_connected"] is True, "Adobe must be connected for iteration 4"
    assert d.get("adobe_user"), "adobe_user object should be present"
    assert d["adobe_user"].get("email"), "adobe_user.email should be populated"


# ---------- invalid url graceful ----------
def test_invalid_url_fails_gracefully():
    r = requests.post(
        f"{API}/analyses",
        data={"frameio_url": "https://not-real.example/foo", "auto_post": "false"},
        timeout=30,
    )
    assert r.status_code == 200
    final = _poll(r.json()["id"], max_wait=60)
    assert final["status"] == "failed"
    assert final.get("error")


# ---------- share-link Adobe post (the BUG FIX) ----------
@pytest.fixture(scope="module")
def share_analysis():
    r = requests.post(
        f"{API}/analyses",
        data={"frameio_url": SHARE_URL, "auto_post": "true"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    final = _poll(aid, max_wait=240)
    assert final is not None
    return final


def test_share_link_pipeline_done(share_analysis):
    a = share_analysis
    assert a["status"] == "done", f"pipeline failed: {a.get('error')!r}"
    assert a["progress"] == 100
    assert a.get("frameio_asset_id"), "asset id must be resolved"


def test_share_link_has_video_fps(share_analysis):
    fps = share_analysis.get("video_fps")
    assert fps is not None and fps > 0, f"video_fps must be >0, got {fps}"


def test_share_link_issues_detected_and_posted(share_analysis):
    a = share_analysis
    issues = a.get("issues") or []
    assert len(issues) > 0, "expected typos to be detected in test video"
    posted = a.get("posted_count") or 0
    assert posted == len(issues), (
        f"posted_count ({posted}) must equal issues ({len(issues)})"
    )
    assert a.get("post_error") in (None, ""), (
        f"post_error must be None when posting succeeds, got: {a.get('post_error')!r}"
    )
    # every issue should be marked posted
    not_posted = [i for i in issues if not i.get("posted_to_frameio")]
    assert not not_posted, f"{len(not_posted)} issues not marked posted"


# ---------- CSV endpoint ----------
def test_csv_endpoint_share(share_analysis):
    aid = share_analysis["id"]
    r = requests.get(f"{API}/analyses/{aid}/csv", timeout=30)
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/csv" in ct.lower(), f"unexpected content-type: {ct}"
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd.lower()
    assert f"proofio-{aid[:8]}.csv" in cd, cd

    body = r.text
    reader = csv_mod.reader(io.StringIO(body))
    rows = list(reader)
    assert rows, "csv body empty"
    header = rows[0]
    # spec header (allow either 'Timestamp' or 'Timestamp (mm:ss)')
    assert any("Timestamp" in c for c in header)
    expected_cols = {
        "Seconds", "Type", "Severity", "Original", "Suggestion",
        "Explanation", "Source text", "Posted to Frame.io",
    }
    assert expected_cols.issubset(set(header)), (
        f"missing CSV cols. got={header}"
    )

    n_issues = len(share_analysis.get("issues") or [])
    assert len(rows) - 1 == n_issues, (
        f"expected {n_issues} data rows, got {len(rows) - 1}"
    )


def test_csv_404_for_unknown():
    r = requests.get(f"{API}/analyses/does-not-exist-xyz/csv", timeout=30)
    assert r.status_code == 404


# ---------- uploaded video regression ----------
@pytest.fixture(scope="module")
def uploaded_id():
    assert os.path.exists(TEST_VIDEO)
    with open(TEST_VIDEO, "rb") as f:
        r = requests.post(
            f"{API}/analyses",
            data={"auto_post": "false", "transcript": "This are wrng."},
            files={"video": ("test.mp4", f, "video/mp4")},
            timeout=60,
        )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_uploaded_video_pipeline(uploaded_id):
    final = _poll(uploaded_id, max_wait=180)
    assert final["status"] == "done", f"failed: {final.get('error')}"
    assert final["progress"] == 100
    assert final.get("video_fps") and final["video_fps"] > 0


def test_cleanup_uploaded(uploaded_id):
    r = requests.delete(f"{API}/analyses/{uploaded_id}", timeout=30)
    assert r.status_code == 200
