"""Gemini 3 Flash powered OCR + grammar/spelling checker (direct Google API)."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3-flash-preview"


class GeminiQuotaExceeded(Exception):
    """Raised on a 429 with no RetryInfo -- a hard daily-quota exhaustion,
    not worth retrying until it resets. Lets callers tell "we couldn't check
    this" apart from "we checked it and found nothing" -- previously both
    cases silently returned an empty list, so a quota wall partway through a
    video looked identical to a clean pass."""


class GeminiRateLimited(Exception):
    """Raised on a 429 that carries a RetryInfo.retryDelay -- a transient
    per-minute/per-second rate limit, not the hard daily quota. Same HTTP
    status as GeminiQuotaExceeded but a different meaning: this one clears
    itself in seconds, so callers should back off and retry rather than
    give up on the rest of the run."""

    def __init__(self, retry_after: float, *args):
        super().__init__(*args)
        self.retry_after = retry_after


def _retry_delay_seconds(exc: "genai_errors.APIError") -> Optional[float]:
    """A 429's error body follows Google's standard error-detail model: a
    transient rate limit attaches a google.rpc.RetryInfo.retryDelay meant to
    be retried shortly, while a hard daily-quota 429 carries no RetryInfo at
    all (retrying won't help until the quota resets at midnight Pacific).
    That presence/absence is the only reliable way to tell the two apart --
    both are the same HTTP 429 / RESOURCE_EXHAUSTED status."""
    details = getattr(exc, "details", None) or {}
    for item in details.get("details", []) or []:
        if "RetryInfo" not in item.get("@type", ""):
            continue
        m = re.match(r"([\d.]+)s", item.get("retryDelay", ""))
        return float(m.group(1)) if m else 5.0
    return None


def _raise_for_429(exc: "genai_errors.APIError") -> None:
    delay = _retry_delay_seconds(exc)
    if delay is not None:
        raise GeminiRateLimited(delay, str(exc)) from exc
    raise GeminiQuotaExceeded(str(exc)) from exc

FRAME_SYSTEM = (
    "You are a meticulous proofreader for animation/video QA. "
    "Given a single frame from a video, perform OCR to read every visible "
    "piece of text (titles, captions, lower thirds, on-screen UI). For each "
    "distinct text block, check ONLY for spelling and grammar mistakes -- do "
    "not flag punctuation or capitalization issues. "
    "Return ONLY a strict JSON object — no prose, no markdown — of this exact shape:\n"
    "{\"texts\": [{\"original\": str, \"has_error\": bool, \"errors\": "
    "[{\"type\": \"spelling|grammar\", "
    "\"original\": str, \"suggestion\": str, \"explanation\": str}]}]}\n"
    "If the frame contains no readable text, return {\"texts\": []}. "
    "Do not invent text. Do not flag stylistic choices."
)

FRAME_BATCH_SYSTEM = (
    "You are a meticulous proofreader for animation/video QA. You will be "
    "shown several frames from a video, each preceded by a label of the "
    "exact form 'Frame N:' (N is its index). Treat each frame completely "
    "independently -- they are unrelated moments, not a sequence. For EACH "
    "frame, perform OCR to read every visible piece of text (titles, "
    "captions, lower thirds, on-screen UI). For each distinct text block on "
    "that frame, check ONLY for spelling and grammar mistakes -- do not "
    "flag punctuation or capitalization issues. "
    "Return ONLY a strict JSON object — no prose, no markdown — of this "
    "exact shape:\n"
    "{\"frames\": [{\"index\": int, \"texts\": [{\"original\": str, "
    "\"has_error\": bool, \"errors\": [{\"type\": \"spelling|grammar\", "
    "\"original\": str, \"suggestion\": str, \"explanation\": str}]}]}]}\n"
    "Include exactly one entry in \"frames\" for every frame index you were "
    "shown, even if that frame has no readable text (use an empty \"texts\" "
    "list in that case). Do not invent text. Do not flag stylistic choices."
)

TRANSCRIPT_SYSTEM = (
    "You are a meticulous proofreader. Given a transcript or script, check "
    "ONLY for spelling and grammar mistakes -- do not flag punctuation or "
    "capitalization issues. Return ONLY a strict JSON object (no prose, no "
    "markdown) of shape:\n"
    "{\"errors\": [{\"type\": \"spelling|grammar\", "
    "\"original\": str, \"suggestion\": str, \"explanation\": str, "
    "\"context\": str}]}"
)

def parse_allowlist(raw: Optional[str]) -> List[str]:
    """Comma/newline-separated terms -> a deduped list, order preserved."""
    if not raw:
        return []
    parts = re.split(r"[,\n]", raw)
    seen = set()
    terms = []
    for p in parts:
        term = p.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def _with_allowlist(system: str, allowlist: Optional[List[str]]) -> str:
    """Appends a "don't flag these" clause to a system prompt. Belt-and-
    suspenders with the exact-match post-filter in server.py -- this is what
    stops Gemini from *suggesting* a "fix" for an intentional name/term in
    the first place, the filter is the backstop if it ignores this anyway."""
    if not allowlist:
        return system
    terms = ", ".join(allowlist)
    return (
        f"{system}\n\nThe following terms are intentional -- character or "
        f"brand names, slang, project-specific jargon -- and must NOT be "
        f"flagged as spelling errors even if they look unusual or "
        f"misspelled: {terms}."
    )


_client: Optional[genai.Client] = None


def _get_client() -> Optional[genai.Client]:
    global _client
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    if _client is None:
        _client = genai.Client(api_key=key)
    return _client


def _strip_json(raw: str) -> str:
    """Strip markdown code fences if the model wrapped JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```$", "", raw)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    return m.group(0) if m else raw


def _parse_texts_block(texts: list) -> List[dict]:
    out: List[dict] = []
    for block in texts or []:
        if not block.get("has_error"):
            continue
        original_text = block.get("original", "")
        for err in block.get("errors", []) or []:
            out.append(
                {
                    "type": err.get("type", "spelling"),
                    "original": err.get("original") or original_text,
                    "suggestion": err.get("suggestion", ""),
                    "explanation": err.get("explanation", ""),
                    "source_text": original_text,
                }
            )
    return out


async def analyze_frame(
    image_path: str, allowlist: Optional[List[str]] = None
) -> List[dict]:
    """Return a list of error dicts for this frame. Empty list if no issues."""
    client = _get_client()
    if not client:
        logger.error("GEMINI_API_KEY missing")
        return []

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                "Analyze this frame and return the JSON described in the system prompt.",
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            ],
            config=types.GenerateContentConfig(
                system_instruction=_with_allowlist(FRAME_SYSTEM, allowlist),
                response_mime_type="application/json",
            ),
        )
    except genai_errors.APIError as exc:
        if exc.code == 429:
            _raise_for_429(exc)
        logger.warning("Gemini frame call failed: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Gemini frame call failed: %s", exc)
        return []

    raw = response.text or ""
    try:
        data = json.loads(_strip_json(raw))
    except Exception:
        logger.debug("Could not parse Gemini frame response: %s", raw[:200])
        return []

    return _parse_texts_block(data.get("texts", []))


async def analyze_frames_batch(
    image_paths: List[str], allowlist: Optional[List[str]] = None
) -> List[List[dict]]:
    """Same as analyze_frame, but for N frames in a single Gemini request.
    Returns one error-list per input path, same order. Cuts request *count*
    roughly N-fold -- what the free-tier quota actually limits, not token
    volume -- at the cost of a bigger blast radius if the one request fails
    or the model loses track across many frames (mitigated by keeping
    GEMINI_BATCH_SIZE modest in server.py, not by anything in here)."""
    client = _get_client()
    if not client:
        logger.error("GEMINI_API_KEY missing")
        return [[] for _ in image_paths]
    if not image_paths:
        return []

    contents: List[object] = []
    for i, path in enumerate(image_paths):
        with open(path, "rb") as f:
            image_bytes = f.read()
        contents.append(f"Frame {i}:")
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
    contents.append(
        f"Analyze all {len(image_paths)} frames above (indices 0 to "
        f"{len(image_paths) - 1}) and return the JSON described in the "
        "system prompt, with one entry in \"frames\" per index."
    )

    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_with_allowlist(FRAME_BATCH_SYSTEM, allowlist),
                response_mime_type="application/json",
            ),
        )
    except genai_errors.APIError as exc:
        if exc.code == 429:
            _raise_for_429(exc)
        logger.warning("Gemini batch frame call failed: %s", exc)
        return [[] for _ in image_paths]
    except Exception as exc:
        logger.warning("Gemini batch frame call failed: %s", exc)
        return [[] for _ in image_paths]

    raw = response.text or ""
    try:
        data = json.loads(_strip_json(raw))
    except Exception:
        logger.debug("Could not parse Gemini batch response: %s", raw[:200])
        return [[] for _ in image_paths]

    out: List[List[dict]] = [[] for _ in image_paths]
    for frame_result in data.get("frames", []) or []:
        idx = frame_result.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(image_paths)):
            continue
        out[idx] = _parse_texts_block(frame_result.get("texts", []))
    return out


async def analyze_transcript(
    transcript: str, allowlist: Optional[List[str]] = None
) -> List[dict]:
    client = _get_client()
    if not client or not transcript.strip():
        return []

    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[f"Transcript:\n\n{transcript}"],
            config=types.GenerateContentConfig(
                system_instruction=_with_allowlist(TRANSCRIPT_SYSTEM, allowlist),
                response_mime_type="application/json",
            ),
        )
    except genai_errors.APIError as exc:
        if exc.code == 429:
            _raise_for_429(exc)
        logger.warning("Gemini transcript call failed: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Gemini transcript call failed: %s", exc)
        return []

    raw = response.text or ""
    try:
        data = json.loads(_strip_json(raw))
    except Exception:
        logger.debug("Could not parse Gemini transcript response")
        return []
    return data.get("errors", []) or []
