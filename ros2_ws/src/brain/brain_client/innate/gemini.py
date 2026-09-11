#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini vision for skills: images plus a question, one Chat Completions call.

Reached the way the brain is — the Innate proxy (its service key needs "gemini"
access or the proxy returns 403), else ``GEMINI_API_KEY``. Import as
``from innate import gemini``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from brain_client.brain.transport import pick_chat
from brain_client.skills.types import cancellable_sleep
from innate_proxy import ProxyClient

if TYPE_CHECKING:
    from brain_client.brain.transport import ChatTransport

MODEL = "gemini-3.5-flash"


class _Logger(Protocol):
    def warning(self, msg: str) -> None: ...


def make_client() -> ChatTransport | None:
    """A transport for vision calls, or None when nothing is configured."""
    return pick_chat(ProxyClient())[0]


def ask_image(
    client: ChatTransport | None,
    images_b64: str | Sequence[str],
    question: str,
    logger: _Logger | None = None,
    retries: int = 3,
) -> str | None:
    """JPEG(s) + question -> reply text. None if no client / all retries fail.
    images_b64: one base64 string or a list of them — sent in order, so the
    question can refer to them as image 1, image 2, ... Frames go inline as
    data URLs (640x480 JPEGs, at most two per call). Raises SkillCancelled
    between attempts if the run is cancelled."""
    if client is None:
        return None
    if isinstance(images_b64, str):
        images_b64 = [images_b64]
    content: list[dict[str, Any]] = [{"type": "text", "text": question}]
    content += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images_b64]
    body: dict[str, Any] = {
        "model": MODEL,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": content}],
    }
    for attempt in range(retries):
        cancellable_sleep(0)
        try:
            data = client.complete(body, None)
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001 — a flaky vision call must not sink the skill
            if logger:
                logger.warning(f"[gemini] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                cancellable_sleep(2.0 * (attempt + 1))
    return None
