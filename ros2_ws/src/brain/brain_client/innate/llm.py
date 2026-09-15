#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Vision Q&A for skills on the brain's model. Import as ``from innate import llm``.

The model is whatever the robot is set to — the brain's ``llm_model`` setting,
reached the way the brain reaches it (the Innate proxy, a vendor key, or an
``llm_base_url`` server) — unless a skill names its own.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence

from innate_llm import Image, Llm, Message, Provider, Request, Role, Text, configure

from brain_client.skills.llm_config import configured
from brain_client.skills.types import cancellable_sleep
from innate_proxy import ProxyClient

_TIMEOUT_SECS = 60.0
_llms: dict[str, Llm] = {}  # one connection pool per model for the process, not one per skill run


def make_client(model: str | None = None) -> Provider | None:
    """The model, or None if no route to it is configured.

    ``model`` is a ``vendor:name`` spec for a skill that wants its own — a
    cheap fast one for a yes/no look; omitted, it is the robot's default.
    """
    default = configured()
    spec = model or default.model
    if spec not in _llms:
        _llms[spec] = configure(spec, ProxyClient(), base_url=default.base_url, extra_body=default.extra_body)
    return _llms[spec].provider


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
                logger.warning(f"[llm] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                cancellable_sleep(2.0 * (attempt + 1))
    return None
