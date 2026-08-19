"""Gemini 3 Flash powered OCR + grammar/spelling checker (direct Google API)."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3-flash-preview"

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
    except Exception as exc:
        logger.warning("Gemini frame call failed: %s", exc)
        return []

    raw = response.text or ""
    try:
        data = json.loads(_strip_json(raw))
    except Exception:
        logger.debug("Could not parse Gemini frame response: %s", raw[:200])
        return []

    out: List[dict] = []
    for block in data.get("texts", []) or []:
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
