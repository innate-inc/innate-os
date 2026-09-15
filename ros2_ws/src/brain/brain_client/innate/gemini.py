#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Vision Q&A for skills on the brain's model. Import as ``from innate import gemini``.

The name is historical: the model is whatever ``LLM_MODEL`` names (any
pydantic-ai provider, Gemini by default), reached through the Innate proxy or
a vendor key exactly as the brain reaches it.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Sequence

from pydantic_ai.direct import model_request_sync
from pydantic_ai.messages import BinaryContent, ModelRequest, TextPart, UserContent, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from brain_client.brain.llm import pick_model
from brain_client.skills.types import cancellable_sleep
from innate_proxy import ProxyClient

MODEL = os.environ.get("LLM_MODEL", "google:gemini-3.5-flash")
_SETTINGS = ModelSettings(temperature=0.0, timeout=60.0)


def make_client() -> Model | None:
    """The model, or None if no route to it is configured."""
    return pick_model(MODEL, ProxyClient()).model


def ask_image(
    client: Model | None, images_b64: str | Sequence[str], question: str, logger=None, retries: int = 3
) -> str | None:
    """JPEG(s) + question -> reply text. None if no client / all retries fail.
    images_b64: one base64 string or a list of them — sent in order, so the
    question can refer to them as image 1, image 2, ... (640x480 JPEGs, at
    most two per call). Raises SkillCancelled between attempts if the run is
    cancelled."""
    if client is None:
        return None
    if isinstance(images_b64, str):
        images_b64 = [images_b64]
    content: list[UserContent] = [
        question,
        *(BinaryContent(base64.b64decode(b), media_type="image/jpeg") for b in images_b64),
    ]
    request = ModelRequest(parts=[UserPromptPart(content=content)])
    for attempt in range(retries):
        cancellable_sleep(0)
        try:
            response = model_request_sync(client, [request], model_settings=_SETTINGS)
            return "".join(part.content for part in response.parts if isinstance(part, TextPart))
        except Exception as e:  # noqa: BLE001 — a failed attempt is retried, the last one reported
            if logger:
                logger.warning(f"[gemini] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                cancellable_sleep(2.0 * (attempt + 1))
    return None
