"""Iteration 9 backend tests:
- NEW per-issue post endpoint validation paths
- Speed (parallel OCR) for f.io/M6WUQCEn with auto_post=false
- Regressions: bulk post still works (validation), CSV, /api/config, Adobe 404
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://frame-spell-check.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

SEED_ANALYSIS_ID = "9958c1fb-50da-4578-9981-98db44f5cfb3"  # per main agent: 9 issues, 1 posted, auto_post=false
SHARE_URL = "https://f.io/M6WUQCEn"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    return s


# ---------- /api/config ----------
def test_config_only_expected_keys(session):
    r = session.get(f"{API}/config", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert set(data.keys()) == {"llm_configured", "frame_interval", "guest_name"}, data
    assert data["llm_configured"] is True
    assert data["guest_name"] == "Proof.io"


# ---------- Adobe routes are dead ----------
@pytest.mark.parametrize("path", [
    "/auth/adobe/start",
    "/auth/adobe/callback",
    "/auth/adobe/status",
])
def test_adobe_routes_404(session, path):
    r = session.get(f"{API}{path}", timeout=15)
    assert r.status_code == 404, f"{path} -> {r.status_code}"


# ---------- Seed analysis sanity ----------
def test_seed_analysis_state(session):
    r = session.get(f"{API}/analyses/{SEED_ANALYSIS_ID}", timeout=15)
    assert r.status_code == 200
    a = r.json()
    assert a["status"] == "done"
    assert a["auto_post"] is False
    # post_error should be None (user opted out)
    assert a.get("post_error") in (None, ""), f"post_error={a.get('post_error')}"
    assert a.get("posted_count", 0) == 1
    issues = a.get("issues") or []
    assert len(issues) == 9, f"expected 9 issues, got {len(issues)}"
    posted = [i for i in issues if i.get("posted_to_frameio")]
    assert len(posted) == 1
    # password must NOT be returned
    assert "password" not in a or a.get("password") is None
    assert "f.io" in a["frameio_url"]


# ---------- Per-issue post: validation paths ----------
def test_per_issue_post_already_posted(session):
    """The 1 already-posted issue should return {posted:false, already:true}."""
    r = session.get(f"{API}/analyses/{SEED_ANALYSIS_ID}", timeout=15)
    issues = r.json()["issues"]
    posted = next(i for i in issues if i.get("posted_to_frameio"))
    r2 = session.post(
        f"{API}/analyses/{SEED_ANALYSIS_ID}/issues/{posted['id']}/post",
        timeout=30,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body == {"posted": False, "already": True}, body


def test_per_issue_post_unknown_issue_id_404(session):
    r = session.post(
        f"{API}/analyses/{SEED_ANALYSIS_ID}/issues/does-not-exist-xxx/post",
        timeout=15,
    )
    assert r.status_code == 404, r.text
    assert "Issue not found" in r.text


def test_per_issue_post_unknown_analysis_404(session):
    r = session.post(
        f"{API}/analyses/00000000-0000-0000-0000-000000000000/issues/whatever/post",
        timeout=15,
    )
    assert r.status_code == 404


def test_per_issue_post_non_share_link_400(session):
    """Create an analysis with a non-Frame.io URL → both per-issue and bulk
    post should return 400 with the share-link-only message."""
    r = session.post(
        f"{API}/analyses",
        data={"frameio_url": "https://example.com/not-a-share.mp4", "auto_post": "false"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    # Pipeline will mark it failed quickly; that's fine. We don't need to wait.
    # Per-issue post must reject with 400 because frameio_url is not a share link.
    r2 = session.post(
        f"{API}/analyses/{aid}/issues/anything/post", timeout=15
    )
    assert r2.status_code == 400, r2.text
    assert "share link" in r2.text.lower()
    # Bulk post should also reject with 400.
    r3 = session.post(f"{API}/analyses/{aid}/post", timeout=15)
    assert r3.status_code == 400, r3.text
    # cleanup
    session.delete(f"{API}/analyses/{aid}", timeout=15)


# ---------- CSV export still works ----------
def test_csv_export(session):
    r = session.get(f"{API}/analyses/{SEED_ANALYSIS_ID}/csv", timeout=30)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    text = r.text
    lines = text.strip().splitlines()
    assert len(lines) == 10, f"expected 1 header + 9 rows, got {len(lines)}"
    assert "Timestamp" in lines[0]
    # exactly 1 row marked "yes" for posted
    yes_rows = [l for l in lines[1:] if l.rstrip().endswith(",yes")]
    assert len(yes_rows) == 1, f"expected 1 'yes' row, got {len(yes_rows)}"


# ---------- Speed test: parallel OCR ----------
@pytest.mark.timeout(120)
def test_speed_parallel_ocr_under_45s(session):
    """POST /analyses with auto_post=false on f.io/M6WUQCEn must reach status=done in <45s."""
    t0 = time.time()
    r = session.post(
        f"{API}/analyses",
        data={"frameio_url": SHARE_URL, "auto_post": "false"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    elapsed = 0.0
    final = None
    while elapsed < 90:
        time.sleep(2)
        elapsed = time.time() - t0
        g = session.get(f"{API}/analyses/{aid}", timeout=15)
        if g.status_code != 200:
            continue
        a = g.json()
        if a["status"] in ("done", "failed"):
            final = a
            break
    assert final is not None, f"did not finish within 90s (last elapsed={elapsed:.1f}s)"
    print(f"\nSpeed test: status={final['status']} elapsed={elapsed:.1f}s issues={len(final.get('issues') or [])}")
    # Cleanup
    session.delete(f"{API}/analyses/{aid}", timeout=15)
    assert final["status"] == "done", f"status={final['status']} error={final.get('error')}"
    # auto_post=false → 0 posts, no post_error
    assert final.get("posted_count", 0) == 0
    assert final.get("post_error") in (None, "")
    assert elapsed < 45, f"too slow: {elapsed:.1f}s (target <45s)"
