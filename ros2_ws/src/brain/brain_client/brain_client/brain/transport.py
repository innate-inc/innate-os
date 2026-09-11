# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's one wire: Chat Completions against whichever server is configured.

Three ways to the same POST — the Innate proxy (managed, it holds the upstream
key), an operator's own OpenAI-compatible endpoint (a LAN vLLM or Ollama,
NVIDIA's hosted NIM, OpenAI), or Google's OpenAI-compatible layer with
``GEMINI_API_KEY``. A transport only moves the body and never interprets it, so
everything above it is written once for all three.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.core.config import BrainConfig
    from innate_proxy import ProxyClient

GOOGLE_COMPAT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
PROXY_SERVICE = "gemini"
PROXY_CHAT_PATH = "/v1/chat/completions"
CHAT_COMPLETIONS_PATH = "/chat/completions"
LLM_API_KEY_ENV = "LLM_API_KEY"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"

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
    # The same extras both forms merge in, readable so a caller can trace the
    # body that will actually go on the wire (brain/context.py:generate).
    extra_body: dict = field(default_factory=dict)


class Backend(StrEnum):
    """Which way the brain reaches its model (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    DIRECT = "direct"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible server: its ``/v1`` root and its key."""

    base_url: str
    api_key: str = ""

    @classmethod
    def from_config(cls, config: BrainConfig) -> Endpoint | None:
        """The configured endpoint, or None when the brain should fall back to Gemini."""
        base_url = config.llm_base_url.strip().rstrip("/")
        if not base_url:
            return None
        return cls(base_url, os.environ.get(LLM_API_KEY_ENV, "").strip())


def pick_chat(
    proxy: ProxyClient | None, endpoint: Endpoint | None = None, extra_body: dict | None = None
) -> tuple[ChatTransport | None, Backend]:
    """The way to reach a model: a configured endpoint, the Innate proxy, or GEMINI_API_KEY.

    sim/launcher/config.py:resolve_brain_backend predicts this choice from the
    host (it cannot import this module) to label the dashboard; change the
    precedence here and change it there.
    """
    if endpoint is not None:
        return direct_chat(endpoint, extra_body), Backend.DIRECT
    if proxy is not None and proxy.is_available():
        return proxy_chat(proxy, extra_body), Backend.PROXY
    api_key = os.environ.get(GEMINI_API_KEY_ENV, "").strip()
    if api_key:
        return direct_chat(Endpoint(GOOGLE_COMPAT_BASE_URL, api_key), extra_body), Backend.DIRECT
    return None, Backend.UNCONFIGURED


def direct_chat(endpoint: Endpoint, extra_body: dict | None = None) -> ChatTransport:
    """Reach an OpenAI-compatible server with its own key."""
    # One client for the process: reuses the TLS connection across turns
    # instead of a fresh handshake per call. Single-threaded use by
    # construction (one turn at a time on the agent's worker thread).
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    client = httpx.Client(headers=headers, timeout=STREAM_TIMEOUT_SECS)
    url = endpoint.base_url + CHAT_COMPLETIONS_PATH
    extras = extra_body or {}

    def stream(body: dict) -> Chunks:
        with client.stream("POST", url, json=body | extras) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"chat direct: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from _sse_chunks(resp.iter_lines())

    def complete(body: dict, timeout: float | None) -> dict:
        resp = client.post(url, json=body | extras, timeout=COMPLETE_TIMEOUT_SECS if timeout is None else timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"chat direct: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    return ChatTransport(stream=stream, complete=complete, extra_body=extras)


def proxy_chat(proxy: ProxyClient, extra_body: dict | None = None) -> ChatTransport:
    """Reach Gemini through the Innate proxy (the proxy holds the upstream key)."""
    extras = extra_body or {}

    def stream(body: dict) -> Chunks:
        with proxy.request_stream(PROXY_SERVICE, PROXY_CHAT_PATH, json=body | extras) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"chat via proxy: HTTP {resp.status_code}: {resp.read()[:200]!r}")
            yield from _sse_chunks(resp.iter_lines())

    def complete(body: dict, timeout: float | None) -> dict:
        with proxy.request_stream(PROXY_SERVICE, PROXY_CHAT_PATH, json=body | extras, timeout=timeout) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise RuntimeError(f"chat via proxy: HTTP {resp.status_code}: {payload[:200]!r}")
            return json.loads(payload) if payload else {}

    return ChatTransport(stream=stream, complete=complete, extra_body=extras)


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
