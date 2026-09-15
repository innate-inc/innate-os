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
import time
from collections.abc import Callable

import httpx

from brain_client.skills.types import cancellable_sleep
from innate_proxy import ProxyClient

SERVICE = "gemini"
ENDPOINT = "/v1/chat/completions"
DIRECT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-3.5-flash"

Client = Callable[[dict], dict]

# Opt-in: no path, no writes. The benchmark sets GEMINI_USAGE_LOG so it can
# report cost; a normal robot writes nothing and keeps no record.
_USAGE_LOG = os.environ.get("GEMINI_USAGE_LOG", "")


def _meter_vision(model: str, data: dict) -> None:
    """Record a skill-vision call in the same log the turn stream writes to.

    Without this the benchmark's cost figure omits every grasp attempt --
    three image calls each -- and reports a floor as a total. See
    the benchmark harness (patch_vision_meter).
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    if not usage:
        return
    if not _USAGE_LOG:
        return
    try:
        row = {
            "t": round(time.time(), 3),
            "model": model,
            "kind": "vision",
            "prompt": usage.get("prompt_tokens", 0),
            "cached": 0,
            "thoughts": 0,
            "output": usage.get("completion_tokens", 0),
            "total": usage.get("total_tokens", 0),
        }
        with open(_USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001,S110 -- metering must never break a skill
        pass


def make_client() -> Client | None:
    """A chat-completions caller, or None when neither the proxy, GEMINI_API_KEY nor GEMINI_BASE_URL is configured.

    GEMINI_BASE_URL is the keyless seam the benchmark uses: the same variable the
    brain's transport honours, so one setting points both at a local stand-in.
    """
    proxy = ProxyClient()
    if proxy.is_available():
        return _proxy_client(proxy)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        return _direct_client(api_key)
    base_url = os.environ.get("GEMINI_BASE_URL", "").strip()
    return _base_url_client(base_url) if base_url else None


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


def _base_url_client(base_url: str) -> Client:
    url = base_url.rstrip("/") + ENDPOINT

    def complete(body: dict) -> dict:
        resp = httpx.post(url, json=body, timeout=180.0)
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
            data = client(body)
            _meter_vision(MODEL, data)
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001
            if logger:
                logger.warning(f"[gemini] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                cancellable_sleep(2.0 * (attempt + 1))
    return None
