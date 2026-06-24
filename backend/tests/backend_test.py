"""Backend tests for Frame.io QA app."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://frame-spell-check.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"
TEST_VIDEO = "/tmp/test.mp4"


# Config
def test_config_endpoint():
    r = requests.get(f"{API}/config", timeout=30)
    assert r.status_code == 200
    data = r.json()
    assert data["frameio_configured"] is True
    assert data["llm_configured"] is True
    assert data["frame_interval"] == 2.0


# Validation
def test_create_analysis_no_input_returns_400():
    r = requests.post(f"{API}/analyses", data={"auto_post": "true"}, timeout=30)
    assert r.status_code == 400


def test_create_analysis_invalid_url_fails_gracefully():
    r = requests.post(
        f"{API}/analyses",
        data={"frameio_url": "https://not-a-real-frameio-url.example/foo", "auto_post": "false"},
        timeout=30,
    )
    assert r.status_code == 200
    aid = r.json()["id"]

    # poll until failed
    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        gr = requests.get(f"{API}/analyses/{aid}", timeout=30)
        assert gr.status_code == 200
        last = gr.json()
        if last["status"] in ("failed", "done"):
            break
        time.sleep(2)
    assert last["status"] == "failed"
    assert last.get("error")


# Upload + full pipeline
@pytest.fixture(scope="module")
def uploaded_analysis_id():
    assert os.path.exists(TEST_VIDEO), "Test video missing"
    with open(TEST_VIDEO, "rb") as f:
        r = requests.post(
            f"{API}/analyses",
            data={"auto_post": "true", "transcript": "This are a tset transcript with eror."},
            files={"video": ("test.mp4", f, "video/mp4")},
            timeout=60,
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "id" in data
    return data["id"]


def test_uploaded_analysis_pipeline(uploaded_analysis_id):
    aid = uploaded_analysis_id
    deadline = time.time() + 180
    last = None
    progress_seen = set()
    while time.time() < deadline:
        r = requests.get(f"{API}/analyses/{aid}", timeout=30)
        assert r.status_code == 200
        last = r.json()
        progress_seen.add(last.get("progress", 0))
        if last["status"] in ("done", "failed"):
            break
        time.sleep(3)
    print("Final:", last["status"], last.get("progress"), last.get("error"), "frames=", last.get("total_frames"), "issues=", len(last.get("issues") or []))
    assert last["status"] == "done", f"pipeline failed: {last.get('error')}"
    assert last["progress"] == 100
    assert last["total_frames"] > 0
    assert len(progress_seen) > 1  # progress increased


def test_list_analyses_has_issue_count(uploaded_analysis_id):
    r = requests.get(f"{API}/analyses", timeout=30)
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list) and len(items) > 0
    found = [it for it in items if it["id"] == uploaded_analysis_id]
    assert found, "Uploaded analysis missing from list"
    assert "issue_count" in found[0]


def test_manual_post_without_asset_returns_400(uploaded_analysis_id):
    # uploaded video has no frameio_asset_id
    r = requests.post(f"{API}/analyses/{uploaded_analysis_id}/post", timeout=30)
    assert r.status_code == 400


def test_delete_analysis(uploaded_analysis_id):
    r = requests.delete(f"{API}/analyses/{uploaded_analysis_id}", timeout=30)
    assert r.status_code == 200
    assert r.json()["deleted"] == 1
    # verify 404
    g = requests.get(f"{API}/analyses/{uploaded_analysis_id}", timeout=30)
    assert g.status_code == 404
