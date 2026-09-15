# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini's native REST, for the one thing the model layer has no words for.

Every model call goes through pydantic-ai (:mod:`brain_client.brain.llm`).
What remains here is the explicit context cache the memory search builds and
deletes — ``cachedContents`` exists only on Gemini's own API — reached the
same two ways as the model: the Innate proxy (it holds the upstream key and
passes native calls through untouched) or ``GEMINI_API_KEY``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

PROXY_SERVICE = "gemini"
DIRECT_BASE_URL = "https://generativelanguage.googleapis.com"
CACHED_CONTENTS_PATH = "/v1beta/cachedContents"

# The backend has no passthrough for this endpoint at all — callers latch the
# feature off permanently rather than retry.
UNSUPPORTED_ENDPOINT_STATUSES = (404, 405, 501)


class GeminiHttpError(RuntimeError):
    """A non-200 from the Gemini API, keeping the status for policy decisions."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class RestPost(Protocol):
    """(api path, body) -> parsed response; raises GeminiHttpError on non-200.

    `timeout` overrides the transport's default deadline for one call.
    """

    def __call__(self, path: str, body: dict, timeout: float | None = None) -> dict: ...


@dataclass(frozen=True)
class GeminiRest:
    """Blocking JSON calls against Gemini's native API."""

    post: RestPost
    delete: Callable[[str], dict]  # api path -> parsed response (usually empty)


def pick_rest(proxy: ProxyClient | None) -> GeminiRest | None:
    """Native Gemini access: the Innate proxy (managed) or GEMINI_API_KEY (dev)."""
    if proxy is not None and proxy.is_available():
        return proxy_rest(proxy)
    api_key = gemini_api_key()
    if api_key:
        return direct_rest(api_key)
    return None


def gemini_api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def proxy_rest(proxy: ProxyClient) -> GeminiRest:
    def request(method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        with proxy.request_stream(PROXY_SERVICE, path, method=method, json=body, timeout=timeout) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise GeminiHttpError(resp.status_code, payload[:200].decode(errors="replace"))
            return json.loads(payload) if payload else {}

    return GeminiRest(
        post=lambda path, body, timeout=None: request("POST", path, body, timeout=timeout),
        delete=lambda path: request("DELETE", path),
    )


def direct_rest(api_key: str) -> GeminiRest:
    # A context-cache build carries a few MB of frames: a longer deadline than a turn.
    client = httpx.Client(headers={"x-goog-api-key": api_key}, timeout=120.0)

    def request(method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        # timeout=None means "no deadline" to httpx, not the client default.
        resp = client.request(
            method, DIRECT_BASE_URL + path, json=body, timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
        )
        if resp.status_code != 200:
            raise GeminiHttpError(resp.status_code, resp.text[:200])
        return resp.json() if resp.content else {}

    return GeminiRest(
        post=lambda path, body, timeout=None: request("POST", path, body, timeout=timeout),
        delete=lambda path: request("DELETE", path),
    )
