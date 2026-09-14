#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini vision for skills, through the Innate proxy or directly with
GEMINI_API_KEY — the same precedence as the brain. Both routes take one
OpenAI-compatible chat-completions body; a proxy service key needs "gemini"
access or the proxy returns 403. Import as ``from innate import gemini``.
"""

import json
import os
from collections.abc import Callable

import httpx

from brain_client.skills.types import cancellable_sleep
from innate_proxy import ProxyClient

SERVICE = "gemini"
ENDPOINT = "/v1/chat/completions"
DIRECT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-3.5-flash"

Client = Callable[[dict], dict]


def make_client() -> Client | None:
    """A chat-completions caller, or None when neither the proxy nor GEMINI_API_KEY is configured."""
    proxy = ProxyClient()
    if proxy.is_available():
        return _proxy_client(proxy)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    return _direct_client(api_key) if api_key else None


def _proxy_client(proxy: ProxyClient) -> Client:
    def complete(body: dict) -> dict:
        with proxy.request_stream(SERVICE, ENDPOINT, method="POST", json=body) as resp:
            resp.raise_for_status()
            return json.loads(resp.read())

    return complete


def _direct_client(api_key: str) -> Client:
    def complete(body: dict) -> dict:
        resp = httpx.post(DIRECT_URL, json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=60.0)
        resp.raise_for_status()
        return resp.json()

    return complete


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
            return client(body)["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001
            if logger:
                logger.warning(f"[gemini] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                cancellable_sleep(2.0 * (attempt + 1))
    return None
