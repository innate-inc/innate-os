#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini vision via Innate proxy (/v1/chat/completions). Service key needs
"gemini" access or the proxy returns 403. Import as ``from innate import gemini``.
"""

import json

from brain_client.skills.types import cancellable_sleep
from innate_proxy import ProxyClient

SERVICE = "gemini"
ENDPOINT = "/v1/chat/completions"
MODEL = "gemini-3.5-flash"


def make_client():
    """ProxyClient, or None if credentials missing."""
    client = ProxyClient()
    return client if client.is_available() else None


def ask_image(client, images_b64, question, logger=None, retries=3):
    """JPEG(s) + question -> reply text. None if no client / all retries fail.
    images_b64: one base64 string or a list of them — sent in order, so the
    question can refer to them as image 1, image 2, ... Frames go inline as
    data URLs (640x480 JPEGs, at most two per call). Raises SkillCancelled
    between attempts if the run is cancelled."""
    if client is None:
        return None
    if isinstance(images_b64, str):
        images_b64 = [images_b64]
    content = [{"type": "text", "text": question}]
    content += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images_b64]
    body = {
        "model": MODEL,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": content}],
    }
    for attempt in range(retries):
        cancellable_sleep(0)
        try:
            with client.request_stream(
                SERVICE,
                ENDPOINT,
                method="POST",
                json=body,
            ) as resp:
                resp.raise_for_status()
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001
            if logger:
                logger.warning(f"[gemini] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                cancellable_sleep(2.0 * (attempt + 1))
    return None
