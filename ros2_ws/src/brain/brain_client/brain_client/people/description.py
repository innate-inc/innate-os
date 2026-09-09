# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The one-line appearance description a person's profile carries.

Written once from a 384 px crop (258 image tokens) and refreshed only when the
appearance changes markedly. The prompt asks for clothing, hair, glasses and an
age band and for nothing else: emotion, ethnicity, gender, health and mood are
outside what this robot is allowed to infer (RFC section 10, EU AI Act Art. 5),
and the model is told so rather than trusted to know.

The description is context for the agent and the owner. It is never an
identifier: recognition happens on templates the model never sees.

Pure except for the crop helper, which uses cv2 like ``memory/quality.py``. No
rclpy, no network of its own — the transport is injected.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import cv2
import numpy as np

from brain_client.brain.transport import GENERATE_PATH

if TYPE_CHECKING:
    from brain_client.people.scribe import Transport
    from brain_client.people.types import Box

CROP_PX = 384
DESCRIPTION_TIMEOUT_SEC = 20.0
MAX_DESCRIPTION_CHARS = 120

SYSTEM_TEXT = (
    "You write one short line describing how a person in a photo looks, so a small home robot can "
    "tell them apart from the other people in the house. Describe ONLY: clothing, hair, whether "
    "they wear glasses, and a broad age band (child, teenager, 20s, 30s, 40s, 50s, 60s, older). "
    "Never mention or infer emotion, mood, expression, ethnicity, skin colour, nationality, "
    "health, disability, attractiveness, or gender. At most 12 words, one line, no full sentences "
    "needed: '30s, glasses, blue jumper, short dark hair.' If the crop is too blurred or too small "
    "to describe, return an empty description."
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "description": {"type": "STRING", "description": "at most 12 words, empty when the crop is unusable"}
    },
    "required": ["description"],
}


def build_request(jpeg: bytes) -> dict:
    """The one-shot description call for a 384 px crop."""
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_TEXT}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
                    {"text": "Describe this person in one short line."},
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }


def parse_description(response: dict) -> str | None:
    """The description, or None when the answer was unreadable or the model
    refused the crop — a missing description is better than a made-up one."""
    try:
        parts = response["candidates"][0]["content"]["parts"]
        text = next(part["text"] for part in parts if part.get("text") and not part.get("thought"))
        data = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError, StopIteration):
        return None
    if not isinstance(data, dict):
        return None
    description = str(data.get("description") or "").strip()
    return description[:MAX_DESCRIPTION_CHARS] or None


def describe(transport: Transport, jpeg: bytes, *, model: str, timeout: float = DESCRIPTION_TIMEOUT_SEC) -> str | None:
    """Blocking: one call, one line. None on any failure — a person without a
    description is normal, and the caller retries on the next good crop."""
    try:
        response = transport(GENERATE_PATH.format(model=model), build_request(jpeg), timeout)
    except Exception:  # noqa: BLE001 — Gemini being down costs a description, nothing else
        return None
    return parse_description(response)


def crop_for_description(frame_bgr: np.ndarray, box: Box, *, size: int = CROP_PX, quality: int = 85) -> bytes | None:
    """A JPEG of the person's box, longest side ``size``, keeping the aspect
    ratio the un-squashed frame has. None when the box lands outside the frame
    or is too small to be worth a call."""
    height, width = frame_bgr.shape[:2]
    top = max(0, min(height - 1, round(box[0] * height)))
    left = max(0, min(width - 1, round(box[1] * width)))
    bottom = max(top + 1, min(height, round(box[2] * height)))
    right = max(left + 1, min(width, round(box[3] * width)))
    crop = frame_bgr[top:bottom, left:right]
    if crop.shape[0] < 16 or crop.shape[1] < 8:
        return None
    scale = size / max(crop.shape[0], crop.shape[1])
    if scale < 1.0:
        target = (max(1, round(crop.shape[1] * scale)), max(1, round(crop.shape[0] * scale)))
        crop = cv2.resize(crop, target, interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buffer.tobytes() if ok else None
