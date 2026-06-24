"""Gemini 3 Flash powered OCR + grammar/spelling checker."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import List, Optional

from emergentintegrations.llm.chat import (
    FileContentWithMimeType,
    LlmChat,
    UserMessage,
)

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3-flash-preview"
GEMINI_PROVIDER = "gemini"

FRAME_SYSTEM = (
    "You are a meticulous proofreader for animation/video QA. "
    "Given a single frame from a video, perform OCR to read every visible "
    "piece of text (titles, captions, lower thirds, on-screen UI). For each "
    "distinct text block, check for spelling AND grammar mistakes. "
    "Return ONLY a strict JSON object — no prose, no markdown — of this exact shape:\n"
    "{\"texts\": [{\"original\": str, \"has_error\": bool, \"errors\": "
    "[{\"type\": \"spelling|grammar|punctuation|capitalization\", "
    "\"original\": str, \"suggestion\": str, \"explanation\": str}]}]}\n"
    "If the frame contains no readable text, return {\"texts\": []}. "
    "Do not invent text. Do not flag stylistic choices."
)

TRANSCRIPT_SYSTEM = (
    "You are a meticulous proofreader. Given a transcript or script, return "
    "ONLY a strict JSON object (no prose, no markdown) of shape:\n"
    "{\"errors\": [{\"type\": \"spelling|grammar|punctuation|capitalization\", "
    "\"original\": str, \"suggestion\": str, \"explanation\": str, "
    "\"context\": str}]}"
)


def _key() -> str:
    return os.environ.get("EMERGENT_LLM_KEY", "")


def _strip_json(raw: str) -> str:
    """Strip markdown code fences if the model wrapped JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```$", "", raw)
    # find the first { ... matching }
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    return m.group(0) if m else raw


async def analyze_frame(image_path: str) -> List[dict]:
    """Return a list of error dicts for this frame. Empty list if no issues."""
    if not _key():
        logger.error("EMERGENT_LLM_KEY missing")
        return []
    chat = LlmChat(
        api_key=_key(),
        session_id=f"frame-{uuid.uuid4()}",
        system_message=FRAME_SYSTEM,
    ).with_model(GEMINI_PROVIDER, GEMINI_MODEL)

    file_content = FileContentWithMimeType(
        file_path=image_path, mime_type="image/jpeg"
    )
    msg = UserMessage(
        text="Analyze this frame and return the JSON described in the system prompt.",
        file_contents=[file_content],
    )
    try:
        response = await chat.send_message(msg)
    except Exception as exc:
        logger.warning("Gemini frame call failed: %s", exc)
        return []

    raw = response if isinstance(response, str) else str(response)
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


async def analyze_transcript(transcript: str) -> List[dict]:
    if not _key() or not transcript.strip():
        return []
    chat = LlmChat(
        api_key=_key(),
        session_id=f"transcript-{uuid.uuid4()}",
        system_message=TRANSCRIPT_SYSTEM,
    ).with_model(GEMINI_PROVIDER, GEMINI_MODEL)

    try:
        response = await chat.send_message(
            UserMessage(text=f"Transcript:\n\n{transcript}")
        )
    except Exception as exc:
        logger.warning("Gemini transcript call failed: %s", exc)
        return []

    raw = response if isinstance(response, str) else str(response)
    try:
        data = json.loads(_strip_json(raw))
    except Exception:
        logger.debug("Could not parse Gemini transcript response")
        return []
    return data.get("errors", []) or []
