#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Vision Q&A for skills on the brain's model. Import as ``from innate import gemini``.

The name is historical: the model is whatever ``LLM_MODEL`` names (any vendor
the brain speaks, Gemini by default), reached the way the brain reaches it —
the Innate proxy, a vendor key, or an ``LLM_BASE_URL`` server. Skills run in
their own process, so the route comes from the environment the stack was
launched with; a ``llm_model`` override kept only in settings.yaml does not
reach here.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Sequence

from brain_client.llm import Image, Llm, Message, Provider, Request, Role, Text, configure
from brain_client.llm.configure import DEFAULT_MODEL
from brain_client.skills.types import cancellable_sleep
from innate_proxy import ProxyClient

MODEL = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
_TIMEOUT_SECS = 60.0
_llm: Llm | None = None  # one connection pool for the process, not one per skill run


def make_client() -> Provider | None:
    """The model, or None if no route to it is configured."""
    global _llm
    if _llm is None:
        _llm = configure(
            MODEL,
            ProxyClient(),
            base_url=os.environ.get("LLM_BASE_URL", ""),
            extra_body=os.environ.get("LLM_EXTRA_BODY", ""),
        )
    return _llm.provider


def ask_image(
    client: Provider | None, images_b64: str | Sequence[str], question: str, logger=None, retries: int = 3
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
    message = Message(Role.USER, (Text(question), *(Image(base64.b64decode(b)) for b in images_b64)))
    request = Request(system="", messages=(message,), temperature=0.0)
    for attempt in range(retries):
        cancellable_sleep(0)
        try:
            return client.run(request, timeout=_TIMEOUT_SECS).message.text()
        except Exception as e:  # noqa: BLE001 — a failed attempt is retried, the last one reported
            if logger:
                logger.warning(f"[gemini] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                cancellable_sleep(2.0 * (attempt + 1))
    return None
