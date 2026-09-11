# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""How the brain reaches an OpenAI-compatible server: straight at OPENAI_COMPAT_BASE_URL.

There is no proxy path on purpose — the endpoint is the operator's own (a
NIM on their network, NVIDIA's hosted API with their key), so nothing of
Innate's stands between the robot and it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

import httpx

BASE_URL_ENV = "OPENAI_COMPAT_BASE_URL"
API_KEY_ENV = "OPENAI_COMPAT_API_KEY"
CHAT_COMPLETIONS_PATH = "/chat/completions"

Transport = Callable[[dict], Iterator[dict]]
"""request body -> streamed response chunks (the model rides in the body)."""


@dataclass(frozen=True)
class Endpoint:
    """Where the server is: the ``.../v1`` root, and a bearer key if it wants one."""

    base_url: str
    api_key: str = ""

    @classmethod
    def from_env(cls) -> Endpoint | None:
        base_url = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
        if not base_url:
            return None
        return cls(base_url, os.environ.get(API_KEY_ENV, "").strip())


def direct_transport(endpoint: Endpoint) -> Transport:
    # One client for the process: reuses the TLS connection across turns
    # instead of a fresh handshake per generate call. Single-threaded use by
    # construction (one turn at a time on the agent's worker thread).
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    client = httpx.Client(headers=headers, timeout=90.0)
    url = endpoint.base_url + CHAT_COMPLETIONS_PATH

    def stream(body: dict) -> Iterator[dict]:
        with client.stream("POST", url, json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"{endpoint.base_url}: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from _sse_chunks(resp.iter_lines())

    return stream


def _sse_chunks(lines: Iterable[str]) -> Iterator[dict]:
    """The JSON frames of a Chat Completions stream; ``data: [DONE]`` ends it."""
    for line in lines:
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":
            return
        chunk = json.loads(payload)
        if isinstance(chunk, dict):
            yield chunk
