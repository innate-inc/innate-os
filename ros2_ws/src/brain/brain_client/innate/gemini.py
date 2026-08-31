#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini vision via Innate proxy (/v1/chat/completions). Service key needs
"gemini" access or the proxy returns 403. Import as ``from innate import gemini``.
"""

import json
import os
import time

from brain_client.skills.types import cancellable_sleep
from innate_proxy import ProxyClient

SERVICE = "gemini"
ENDPOINT = "/v1/chat/completions"
MODEL = "gemini-3.5-flash"


_USAGE_LOG = os.environ.get("GEMINI_USAGE_LOG", "/root/innate-os/workspace/gemini_usage.jsonl")


def _meter_vision(model: str, data: dict) -> None:
    """Record a skill-vision call in the same log the turn stream writes to.

    Without this the benchmark's cost figure omits every grasp attempt --
    three image calls each -- and reports a floor as a total. See
    the benchmark harness (patch_vision_meter).
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    if not usage:
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


class _DirectClient:
    """Same request_stream shape as ProxyClient, against a plain base URL.

    Skills reach vision through the Innate proxy, while the brain reaches its
    model through brain/transport.py. Those are two independent seams, so a dev
    setup with no service key gets a working brain and skills that still fail
    with "Innate proxy not configured". GEMINI_BASE_URL now covers both.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def is_available(self) -> bool:
        return True

    def request_stream(self, _service, endpoint, method="POST", **kwargs):
        import httpx

        client = httpx.Client(timeout=180.0)
        return client.stream(method, self.base_url + endpoint, **kwargs)


def make_client():
    """ProxyClient, or a direct client when GEMINI_BASE_URL is set, else None."""
    client = ProxyClient()
    if client.is_available():
        return client
    base = os.environ.get("GEMINI_BASE_URL", "").strip()
    return _DirectClient(base) if base else None


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
            _meter_vision(MODEL, data)
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001
            if logger:
                logger.warning(f"[gemini] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                cancellable_sleep(2.0 * (attempt + 1))
    return None
