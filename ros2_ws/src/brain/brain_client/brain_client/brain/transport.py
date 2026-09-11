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


class ChatHttpError(RuntimeError):
    """A non-200 from a chat server, keeping the status for policy decisions."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


@dataclass(frozen=True)
class ChatTransport:
    """Chat Completions against one server: the streamed and the blocking form."""

    stream: Callable[[dict], Chunks]
    complete: Callable[[dict, float | None], dict]


class Backend(StrEnum):
    """Which way the brain reaches its model (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    DIRECT = "direct"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible server: its ``/v1`` root, its key, its extras."""

    base_url: str
    api_key: str = ""
    extra_body: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: BrainConfig, logger: RcutilsLogger) -> Endpoint | None:
        """The configured endpoint, or None when the brain should fall back to Gemini."""
        base_url = config.llm_base_url.strip().rstrip("/")
        if not base_url:
            return None
        return cls(base_url, os.environ.get(LLM_API_KEY_ENV, "").strip(), _extra_body(config.llm_extra_body, logger))


def pick_chat(proxy: ProxyClient | None, endpoint: Endpoint | None = None) -> tuple[ChatTransport | None, Backend]:
    """The way to reach a model: a configured endpoint, the Innate proxy, or GEMINI_API_KEY.

    sim/launcher/config.py:resolve_brain_backend predicts this choice from the
    host (it cannot import this module) to label the dashboard; change the
    precedence here and change it there.
    """
    if endpoint is not None:
        return direct_chat(endpoint), Backend.DIRECT
    if proxy is not None and proxy.is_available():
        return proxy_chat(proxy), Backend.PROXY
    api_key = os.environ.get(GEMINI_API_KEY_ENV, "").strip()
    if api_key:
        return direct_chat(Endpoint(GOOGLE_COMPAT_BASE_URL, api_key)), Backend.DIRECT
    return None, Backend.UNCONFIGURED


def direct_chat(endpoint: Endpoint) -> ChatTransport:
    """Reach an OpenAI-compatible server with its own key."""
    # One client for the process: reuses the TLS connection across turns
    # instead of a fresh handshake per call. Single-threaded use by
    # construction (one turn at a time on the agent's worker thread).
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    client = httpx.Client(headers=headers, timeout=STREAM_TIMEOUT_SECS)
    url = endpoint.base_url + CHAT_COMPLETIONS_PATH

    def stream(body: dict) -> Chunks:
        with client.stream("POST", url, json=body | endpoint.extra_body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"chat direct: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from _sse_chunks(resp.iter_lines())

    def complete(body: dict, timeout: float | None) -> dict:
        resp = client.post(
            url, json=body | endpoint.extra_body, timeout=COMPLETE_TIMEOUT_SECS if timeout is None else timeout
        )
        if resp.status_code != 200:
            raise ChatHttpError(resp.status_code, resp.text[:200])
        return resp.json() if resp.content else {}

    return ChatTransport(stream=stream, complete=complete)


def proxy_chat(proxy: ProxyClient) -> ChatTransport:
    """Reach Gemini through the Innate proxy (the proxy holds the upstream key)."""

    def stream(body: dict) -> Chunks:
        with proxy.request_stream(PROXY_SERVICE, PROXY_CHAT_PATH, json=body) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"chat via proxy: HTTP {resp.status_code}: {resp.read()[:200]!r}")
            yield from _sse_chunks(resp.iter_lines())

    def complete(body: dict, timeout: float | None) -> dict:
        with proxy.request_stream(PROXY_SERVICE, PROXY_CHAT_PATH, json=body, timeout=timeout) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise ChatHttpError(resp.status_code, payload[:200].decode(errors="replace"))
            return json.loads(payload) if payload else {}

    return ChatTransport(stream=stream, complete=complete)


def _extra_body(raw: str, logger: RcutilsLogger) -> dict:
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
