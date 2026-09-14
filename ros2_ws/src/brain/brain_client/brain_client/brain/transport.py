# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's one wire: Chat Completions against whichever server is configured.

Three ways to the same POST — the Innate proxy (managed, it holds the upstream
key), an operator's own OpenAI-compatible endpoint (a LAN vLLM or Ollama,
NVIDIA's hosted NIM, OpenAI), or Google's OpenAI-compatible layer with
``GEMINI_API_KEY``. A transport only moves the body and never interprets it, so
everything above it is written once for all three.

Beside the chat wire, and only when it reaches Gemini, rides Gemini's native
REST for the one thing Chat Completions has no words for: creating and deleting
the explicit context caches the memory search references from its requests.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from brain_client.common.enums import StrEnum
from brain_client.core.config import GEMINI_ROUTE

if TYPE_CHECKING:
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from innate_proxy import ProxyClient

GOOGLE_API_ROOT = "https://generativelanguage.googleapis.com"
GOOGLE_COMPAT_BASE_URL = GOOGLE_API_ROOT + "/v1beta/openai"
CACHED_CONTENTS_PATH = "/v1beta/cachedContents"
PROXY_SERVICE = "gemini"
PROXY_CHAT_PATH = "/v1/chat/completions"
CHAT_COMPLETIONS_PATH = "/chat/completions"
LLM_API_KEY_ENV = "LLM_API_KEY"
MEMORY_LLM_API_KEY_ENV = "MEMORY_LLM_API_KEY"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"

# The backend has no passthrough for this endpoint at all — callers latch the
# feature off permanently rather than retry.
UNSUPPORTED_ENDPOINT_STATUSES = (404, 405, 501)

# A streamed turn must outlive a slow first token; a blocking call carries the
# whole reply (a memory search ships every remembered frame) and gets longer.
STREAM_TIMEOUT_SECS = 90.0
COMPLETE_TIMEOUT_SECS = 120.0

Chunks = Iterator[dict]
"""Parsed SSE chunks of one streamed completion."""


@dataclass(frozen=True)
class ChatTransport:
    """Chat Completions against one server: the streamed and the blocking form."""

    stream: Callable[[dict], Chunks]
    complete: Callable[[dict, float | None], dict]


@dataclass(frozen=True)
class GeminiRest:
    """Gemini's native REST on the same route the chat wire takes: (api path, body)
    -> parsed response and (api path) -> parsed response, raising
    :class:`ChatRejected` on a non-200."""

    post: Callable[[str, dict], dict]
    delete: Callable[[str], dict]


@dataclass(frozen=True)
class Wire:
    """One way to a model: the chat transport (None when nothing is configured),
    how it was chosen, and Gemini's native REST when the route reaches Gemini."""

    chat: ChatTransport | None
    backend: Backend
    gemini: GeminiRest | None = None


# How servers word a request that outgrew their window or image cap — vLLM/NIM,
# OpenAI ("maximum context length"), Google's compat layer ("input token count
# ... exceeds the maximum number of input tokens") and any 413.
_TOO_LARGE = re.compile(
    r"context (?:length|window)|maximum number of (?:input )?tokens|token count|too many (?:tokens|images)"
    r"|at most \d+ image|payload too large|request entity too large",
    re.IGNORECASE,
)


class ChatRejected(RuntimeError):
    """The server answered with an error status. ``too_large`` singles out the one
    rejection a smaller request can fix, from auth, model and field errors that
    no shrinking will."""

    def __init__(self, where: str, status: int, text: str):
        super().__init__(f"{where}: HTTP {status}: {text[:200]}")
        self.status = status
        self.too_large = status == 413 or (status == 400 and _TOO_LARGE.search(text) is not None)


class Backend(StrEnum):
    """Which way the brain reaches its model (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    DIRECT = "direct"  # an endpoint of the operator's own
    GEMINI_DIRECT = "gemini-direct"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible server: its ``/v1`` root and its key."""

    base_url: str
    api_key: str = ""

    @classmethod
    def parse(cls, base_url: str, key_env: str) -> Endpoint | None:
        """A configured ``/v1`` root with its key from the environment, or None
        when the setting is blank and the route falls back to Gemini."""
        base_url = base_url.strip().rstrip("/")
        if not base_url:
            return None
        return cls(base_url, os.environ.get(key_env, "").strip())


def pick_wire(proxy: ProxyClient | None, endpoint: Endpoint | None = None) -> Wire:
    """The way to reach a model: a configured endpoint, the Innate proxy, or GEMINI_API_KEY.

    sim/launcher/config.py:resolve_brain_backend predicts this choice from the
    host (it cannot import this module) to label the dashboard; change the
    precedence here and change it there.
    """
    if endpoint is not None:
        at_google = endpoint.base_url.startswith(GOOGLE_COMPAT_BASE_URL)
        return Wire(direct_chat(endpoint), Backend.DIRECT, direct_rest(endpoint.api_key) if at_google else None)
    if proxy is not None and proxy.is_available():
        return Wire(proxy_chat(proxy), Backend.PROXY, proxy_rest(proxy))
    api_key = os.environ.get(GEMINI_API_KEY_ENV, "").strip()
    if api_key:
        return Wire(direct_chat(Endpoint(GOOGLE_COMPAT_BASE_URL, api_key)), Backend.GEMINI_DIRECT, direct_rest(api_key))
    return Wire(None, Backend.UNCONFIGURED)


def pick_memory_wire(proxy: ProxyClient | None, route: str, brain: Wire) -> Wire:
    """The memory search's wire from ``memory_llm_base_url``: blank rides the
    brain's own, ``gemini`` the managed Gemini route, anything else an endpoint
    of its own with ``MEMORY_LLM_API_KEY``."""
    route = route.strip()
    if not route:
        return brain
    if route == GEMINI_ROUTE:
        return pick_wire(proxy)
    return pick_wire(proxy, Endpoint.parse(route, MEMORY_LLM_API_KEY_ENV))


def direct_chat(endpoint: Endpoint) -> ChatTransport:
    """Reach an OpenAI-compatible server with its own key."""
    # One client for the process: reuses the TLS connection across calls. The agent's
    # worker thread and the memory search's spin thread share it — httpx.Client is thread-safe.
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    client = httpx.Client(headers=headers, timeout=STREAM_TIMEOUT_SECS)
    url = endpoint.base_url + CHAT_COMPLETIONS_PATH

    def stream(body: dict) -> Chunks:
        with client.stream("POST", url, json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise ChatRejected("chat direct", resp.status_code, resp.text)
            yield from _sse_chunks(resp.iter_lines())

    def complete(body: dict, timeout: float | None) -> dict:
        resp = client.post(url, json=body, timeout=COMPLETE_TIMEOUT_SECS if timeout is None else timeout)
        if resp.status_code != 200:
            raise ChatRejected("chat direct", resp.status_code, resp.text)
        return resp.json() if resp.content else {}

    return ChatTransport(stream=stream, complete=complete)


def proxy_chat(proxy: ProxyClient) -> ChatTransport:
    """Reach Gemini through the Innate proxy (the proxy holds the upstream key)."""

    def stream(body: dict) -> Chunks:
        with proxy.request_stream(PROXY_SERVICE, PROXY_CHAT_PATH, json=body, timeout=STREAM_TIMEOUT_SECS) as resp:
            if resp.status_code != 200:
                raise ChatRejected("chat via proxy", resp.status_code, repr(resp.read()[:200]))
            yield from _sse_chunks(resp.iter_lines())

    def complete(body: dict, timeout: float | None) -> dict:
        deadline = COMPLETE_TIMEOUT_SECS if timeout is None else timeout
        with proxy.request_stream(PROXY_SERVICE, PROXY_CHAT_PATH, json=body, timeout=deadline) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise ChatRejected("chat via proxy", resp.status_code, repr(payload[:200]))
            return json.loads(payload) if payload else {}

    return ChatTransport(stream=stream, complete=complete)


def direct_rest(api_key: str) -> GeminiRest:
    """Gemini's native REST directly against Google with its key."""
    # A cache build carries every remembered frame: the blocking deadline, not the stream's.
    client = httpx.Client(headers={"x-goog-api-key": api_key}, timeout=COMPLETE_TIMEOUT_SECS)

    def request(method: str, path: str, body: dict | None = None) -> dict:
        resp = client.request(method, GOOGLE_API_ROOT + path, json=body)
        if resp.status_code != 200:
            raise ChatRejected("gemini direct", resp.status_code, resp.text)
        return resp.json() if resp.content else {}

    return GeminiRest(post=lambda path, body: request("POST", path, body), delete=lambda path: request("DELETE", path))


def proxy_rest(proxy: ProxyClient) -> GeminiRest:
    """Gemini's native REST through the Innate proxy (native paths pass through untouched)."""

    def request(method: str, path: str, body: dict | None = None) -> dict:
        with proxy.request_stream(PROXY_SERVICE, path, method=method, json=body, timeout=COMPLETE_TIMEOUT_SECS) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise ChatRejected("gemini via proxy", resp.status_code, repr(payload[:200]))
            return json.loads(payload) if payload else {}

    return GeminiRest(post=lambda path, body: request("POST", path, body), delete=lambda path: request("DELETE", path))


def parse_extra_body(raw: str, logger: RcutilsLogger) -> dict:
    """The ``llm_extra_body`` setting as a dict, empty when unset or unusable."""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        logger.warn(f"[Brain] llm_extra_body is not valid JSON ({error}) — ignoring it")
        return {}
    if not isinstance(parsed, dict):
        logger.warn("[Brain] llm_extra_body must be a JSON object — ignoring it")
        return {}
    return parsed


def _sse_chunks(lines: Iterable[str]) -> Chunks:
    for line in lines:
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":
            return
        yield json.loads(payload)
