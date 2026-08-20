"""Self-check for the concurrency/queueing fix: shared job queue with
duration-based priority (server.py), and 429 retry-delay classification
(ai_service.py) that tells a transient per-minute rate limit apart from a
hard daily-quota exhaustion. No network, no Mongo, no Gemini call. Run
directly:
    python backend/tests/test_concurrency.py
(also pytest-discoverable, since the functions are named test_*).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from google.genai import errors as genai_errors  # noqa: E402

from services.ai_service import (  # noqa: E402
    GeminiQuotaExceeded,
    GeminiRateLimited,
    _raise_for_429,
)
import server  # noqa: E402


def _fake_429(details_list):
    return genai_errors.APIError(
        429,
        {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "quota exceeded",
            "details": details_list,
        },
    )


def test_retryinfo_429_is_classified_as_transient_rate_limit():
    exc = _fake_429([
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"},
    ])
    try:
        _raise_for_429(exc)
        assert False, "expected an exception"
    except GeminiRateLimited as e:
        assert e.retry_after == 12.0
    except GeminiQuotaExceeded:
        assert False, "a RetryInfo-bearing 429 must not be treated as a hard quota wall"


def test_bare_429_with_no_retryinfo_is_classified_as_hard_quota():
    exc = _fake_429([])
    try:
        _raise_for_429(exc)
        assert False, "expected an exception"
    except GeminiQuotaExceeded:
        pass
    except GeminiRateLimited:
        assert False, "a 429 with no RetryInfo must not be treated as retryable"


def test_enqueue_uses_unknown_duration_default_when_no_local_video():
    """A share-link job's real duration isn't known until it's downloaded
    inside the pipeline, so _enqueue_pipeline must fall back to the fixed
    UNKNOWN_DURATION_PRIORITY_SEC rather than e.g. crashing or treating it
    as 0 (which would wrongly jump every share-link job to the front)."""
    async def run():
        while not server._pipeline_queue.empty():
            server._pipeline_queue.get_nowait()
        server._enqueue_pipeline("share-link-job", None, None)
        priority, _seq, args = server._pipeline_queue.get_nowait()
        assert priority == server.UNKNOWN_DURATION_PRIORITY_SEC
        assert args[0] == "share-link-job"

    asyncio.run(run())


def test_pipeline_queue_runs_shorter_priority_first_and_breaks_ties_fifo():
    """Exercises the real _pipeline_queue / _queue_seq the app uses (same
    tuple shape _enqueue_pipeline produces: (priority, seq, args)), with an
    explicit priority standing in for ffprobe's real duration -- this is
    the actual guarantee the fix is for: a short video must not sit queued
    behind a long one, and jobs of equal priority must still run in the
    order they arrived, not get reordered or crash trying to compare the
    unorderable `args` tuples once priorities tie."""
    async def run():
        while not server._pipeline_queue.empty():
            server._pipeline_queue.get_nowait()

        server._pipeline_queue.put_nowait((600.0, next(server._queue_seq), ("long", None, None)))
        server._pipeline_queue.put_nowait((5.0, next(server._queue_seq), ("short", None, None)))
        server._pipeline_queue.put_nowait((5.0, next(server._queue_seq), ("short2", None, None)))

        _p, _s, args = server._pipeline_queue.get_nowait()
        assert args[0] == "short"
        _p, _s, args = server._pipeline_queue.get_nowait()
        assert args[0] == "short2"
        _p, _s, args = server._pipeline_queue.get_nowait()
        assert args[0] == "long"

    asyncio.run(run())


if __name__ == "__main__":
    test_retryinfo_429_is_classified_as_transient_rate_limit()
    test_bare_429_with_no_retryinfo_is_classified_as_hard_quota()
    test_enqueue_uses_unknown_duration_default_when_no_local_video()
    test_pipeline_queue_runs_shorter_priority_first_and_breaks_ties_fifo()
    print("OK")
