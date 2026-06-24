"""Regression test for Frame.io share link (f.io/...) bug fix.

Bug: pasting a public Frame.io share link `https://f.io/pbR84Qul` used to fail
with 'Frame.io API rejected the request. Token may be invalid...' because the
legacy `fio-u-` token cannot read V4 share contents.

Fix: detect share links and use Playwright to scrape the <video> currentSrc
from the share page, then download from that signed CDN URL.

This test exercises the real share URL end-to-end.
"""
import os
import time

import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL", "https://frame-spell-check.preview.emergentagent.com"
).rstrip("/")
API = f"{BASE_URL}/api"

SHARE_URL = "https://f.io/pbR84Qul"
EXPECTED_FILE_ID = "48055c11-2525-431f-8df3-894a12e0a6d3"


def _poll(aid, max_wait=180):
    deadline = time.time() + max_wait
    last = None
    seen_states = set()
    while time.time() < deadline:
        r = requests.get(f"{API}/analyses/{aid}", timeout=30)
        assert r.status_code == 200
        last = r.json()
        seen_states.add(last.get("status"))
        if last["status"] in ("done", "failed"):
            break
        time.sleep(3)
    return last, seen_states


def test_share_link_resolves_and_completes():
    """f.io share link must produce a 'done' analysis with issues > 0."""
    r = requests.post(
        f"{API}/analyses",
        data={"frameio_url": SHARE_URL, "auto_post": "true"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    aid = r.json()["id"]

    last, seen = _poll(aid, max_wait=180)
    print("Final:", last.get("status"), "progress=", last.get("progress"),
          "asset_id=", last.get("frameio_asset_id"),
          "issues=", len(last.get("issues") or []),
          "posted=", last.get("posted_count"),
          "post_error=", (last.get("post_error") or "")[:80],
          "error=", last.get("error"))

    # MUST NOT fail with the old error
    assert last["status"] == "done", f"Expected done, got {last['status']}: {last.get('error')}"
    assert last["progress"] == 100

    # Correct asset id = LAST UUID (file id), not share id
    assert last["frameio_asset_id"] == EXPECTED_FILE_ID, (
        f"Expected file_id {EXPECTED_FILE_ID}, got {last.get('frameio_asset_id')}"
    )

    # Gemini should find issues (video has intentional typos)
    assert len(last.get("issues") or []) > 0, "Expected Gemini to find spelling/grammar issues"

    # Auto-post for shares: posted_count=0 but post_error must be populated
    assert last.get("posted_count", 0) == 0
    assert last.get("post_error"), "post_error must explain why auto-post failed"
    assert isinstance(last["post_error"], str) and len(last["post_error"]) > 0

    # Cleanup
    requests.delete(f"{API}/analyses/{aid}", timeout=30)


def test_invalid_url_still_fails_gracefully():
    """Non-share, non-UUID URL must end with status=failed and an error message."""
    r = requests.post(
        f"{API}/analyses",
        data={"frameio_url": "https://example.com/no-uuid-here", "auto_post": "false"},
        timeout=30,
    )
    assert r.status_code == 200
    aid = r.json()["id"]
    last, _ = _poll(aid, max_wait=60)
    assert last["status"] == "failed"
    assert last.get("error")
    requests.delete(f"{API}/analyses/{aid}", timeout=30)
