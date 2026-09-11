# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""How the brain reaches its model: an OpenAI-compatible endpoint of the operator's
own, the Innate proxy (managed), or GEMINI_API_KEY (dev).

The proxy holds the upstream key and passes native Gemini calls — the turn
stream and the memory search's blocking generate / context-cache management —
through untouched (the robot authenticates with its service key); the direct
path talks to ``generativelanguage.googleapis.com``. Both speak the same wire
format — a transport only moves payloads and never interprets them. The
OpenAI-compatible exit is the one that does not: it carries the same native
bodies through :mod:`brain_client.brain.openai_compat` to a
``/v1/chat/completions`` server (a vLLM or Ollama on the robot's network,
NVIDIA's hosted NIM), so nothing above this module learns a second wire.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import httpx

from brain_client.brain import openai_compat
from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.core.config import BrainConfig
    from innate_proxy import ProxyClient

PROXY_SERVICE = "gemini"
DIRECT_BASE_URL = "https://generativelanguage.googleapis.com"
STREAM_PATH = "/v1beta/models/{model}:streamGenerateContent?alt=sse"
GENERATE_PATH = "/v1beta/models/{model}:generateContent"
CACHED_CONTENTS_PATH = "/v1beta/cachedContents"
FILES_UPLOAD_PATH = "/upload/v1beta/files"
CHAT_COMPLETIONS_PATH = "/chat/completions"  # under an OpenAI-compatible endpoint's .../v1 root
LLM_API_KEY_ENV = "LLM_API_KEY"

# The backend has no passthrough for this endpoint at all — callers latch the
# feature off permanently rather than retry (shared by the cache and files tiers).
UNSUPPORTED_ENDPOINT_STATUSES = (404, 405, 501)

Transport = Callable[[str, dict], Iterator[dict]]
"""(model, request body) -> streamed response chunks."""


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
    """Blocking JSON + media calls against the same backend the stream uses."""

    post: RestPost
    delete: Callable[[str], dict]  # api path -> parsed response (usually empty)
    upload: Callable[[str, bytes, str], dict]  # (api path, raw bytes, mime type) -> parsed response


class Backend(StrEnum):
    """Which way the brain reaches its model (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    DIRECT = "gemini-direct"
    OPENAI_COMPAT = "openai-compat"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible server the brain thinks with instead of Gemini: its
    ``.../v1`` root, a bearer key if it wants one, and the operator's fields
    merged into every request (server-specific switches such as thinking off)."""

    base_url: str
    api_key: str = ""
    extra_body: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: BrainConfig, logger: RcutilsLogger) -> Endpoint | None:
        """The endpoint the settings name, or None when the brain stays on Gemini.

        The URL and extras are robot settings; the key alone is a secret and
        comes from the environment, like every other credential here.
        """
        base_url = config.llm_base_url.strip().rstrip("/")
        if not base_url:
            return None
        return cls(base_url, os.environ.get(LLM_API_KEY_ENV, "").strip(), _extra_body(config.llm_extra_body, logger))


def _extra_body(raw: str, logger: RcutilsLogger) -> dict:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        logger.warn(f"[Brain] llm_extra_body is not valid JSON ({error}) — sending requests without it")
        return {}
    if not isinstance(parsed, dict):
        logger.warn("[Brain] llm_extra_body must be a JSON object — sending requests without it")
        return {}
    return parsed


def pick_transport(proxy: ProxyClient | None, endpoint: Endpoint | None = None) -> tuple[Transport | None, Backend]:
    """The way to reach the model: an OpenAI-compatible endpoint when one is set
    (the operator's own server, never through the proxy), else the Innate proxy
    (managed), else GEMINI_API_KEY (dev).

    sim/launcher/config.py:resolve_brain_backend predicts this choice from the
    host (it cannot import this module) to label the dashboard; change the
    precedence here and change it there.
    """
    if endpoint is not None:
        return openai_compat_transport(endpoint), Backend.OPENAI_COMPAT
    if proxy is not None and proxy.is_available():
        return proxy_transport(proxy), Backend.PROXY
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        return direct_transport(api_key), Backend.DIRECT
    return None, Backend.UNCONFIGURED


def proxy_transport(proxy: ProxyClient) -> Transport:
    """Reach Gemini through the Innate proxy (the proxy holds the upstream key)."""

    def stream(model: str, body: dict) -> Iterator[dict]:
        endpoint = STREAM_PATH.format(model=model)
        with proxy.request_stream(PROXY_SERVICE, endpoint, json=body) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"gemini via proxy: HTTP {resp.status_code}: {resp.read()[:200]!r}")
            yield from _sse_chunks(resp.iter_lines())

    return stream


def direct_transport(api_key: str) -> Transport:
    """Reach Google's Gemini API directly with GEMINI_API_KEY."""
    # One client for the process: reuses the TLS connection across turns
    # instead of a fresh handshake per generate call. Single-threaded use by
    # construction (one turn at a time on the agent's worker thread).
    client = httpx.Client(headers={"x-goog-api-key": api_key}, timeout=90.0)

    def stream(model: str, body: dict) -> Iterator[dict]:
        url = DIRECT_BASE_URL + STREAM_PATH.format(model=model)
        with client.stream("POST", url, json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"gemini direct: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from _sse_chunks(resp.iter_lines())

    return stream


def pick_rest(proxy: ProxyClient | None, endpoint: Endpoint | None = None) -> GeminiRest | None:
    """Blocking-call access to the model, chosen the same way as :func:`pick_transport`."""
    if endpoint is not None:
        return openai_compat_rest(endpoint)
    if proxy is not None and proxy.is_available():
        return proxy_rest(proxy)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        return direct_rest(api_key)
    return None


def proxy_rest(proxy: ProxyClient) -> GeminiRest:
    """Non-streaming Gemini calls through the proxy (same service passthrough as the stream)."""

    def request(
        method: str, path: str, body: dict | None = None, data: bytes | None = None, timeout: float | None = None
    ) -> dict:
        with proxy.request_stream(PROXY_SERVICE, path, method=method, json=body, data=data, timeout=timeout) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise GeminiHttpError(resp.status_code, payload[:200].decode(errors="replace"))
            return json.loads(payload) if payload else {}

    # The proxy client cannot attach the raw-upload protocol headers; a
    # passthrough that requires them answers non-200 and the caller latches
    # the files tier off (frames ride inline — today's behavior).
    return GeminiRest(
        post=lambda path, body, timeout=None: request("POST", path, body, timeout=timeout),
        delete=lambda path: request("DELETE", path),
        upload=lambda path, data, mime: request("POST", path, data=data),
    )


def direct_rest(api_key: str) -> GeminiRest:
    """Non-streaming Gemini calls directly against Google with GEMINI_API_KEY."""
    # Own client: a context-cache upload carries a few MB of frames and needs a
    # longer timeout than the per-chunk streaming client.
    client = httpx.Client(headers={"x-goog-api-key": api_key}, timeout=120.0)

    def request(method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        # timeout=None means "no deadline" to httpx, not the client default.
        resp = client.request(
            method, DIRECT_BASE_URL + path, json=body, timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
        )
        if resp.status_code != 200:
            raise GeminiHttpError(resp.status_code, resp.text[:200])
        return resp.json() if resp.content else {}

    def upload(path: str, data: bytes, mime: str) -> dict:
        resp = client.post(
            DIRECT_BASE_URL + path,
            content=data,
            headers={"X-Goog-Upload-Protocol": "raw", "Content-Type": mime},
        )
        if resp.status_code != 200:
            raise GeminiHttpError(resp.status_code, resp.text[:200])
        return resp.json() if resp.content else {}

    return GeminiRest(
        post=lambda path, body, timeout=None: request("POST", path, body, timeout=timeout),
        delete=lambda path: request("DELETE", path),
        upload=upload,
    )


def openai_compat_transport(endpoint: Endpoint) -> Transport:
    """Reach an OpenAI-compatible server with the native bodies translated on the way."""
    client = _openai_compat_client(endpoint, timeout=90.0)
    url = endpoint.base_url + CHAT_COMPLETIONS_PATH

    def stream(model: str, body: dict) -> Iterator[dict]:
        request = openai_compat.encode_request(model, body, stream=True, extra_body=endpoint.extra_body)
        with client.stream("POST", url, json=request) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"{endpoint.base_url}: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from openai_compat.decode_stream(_sse_chunks(resp.iter_lines()))

    return stream


def openai_compat_rest(endpoint: Endpoint) -> GeminiRest:
    """Blocking generate calls against an OpenAI-compatible server.

    Only ``generateContent`` has a counterpart there. The context-cache and
    Files API paths answer 501, which their callers already read as "this
    backend has no such tier" and latch off — frames ride inline.
    """
    client = _openai_compat_client(endpoint, timeout=120.0)
    url = endpoint.base_url + CHAT_COMPLETIONS_PATH

    def post(path: str, body: dict, timeout: float | None = None) -> dict:
        model = openai_compat.generate_model(path)
        if model is None:
            raise GeminiHttpError(501, f"{path} has no counterpart on an OpenAI-compatible server")
        request = openai_compat.encode_request(model, body, stream=False, extra_body=endpoint.extra_body)
        resp = client.post(url, json=request, timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout)
        if resp.status_code != 200:
            raise GeminiHttpError(resp.status_code, resp.text[:200])
        return openai_compat.decode_response(resp.json())

    def upload(path: str, data: bytes, mime: str) -> dict:
        raise GeminiHttpError(501, f"{path} has no counterpart on an OpenAI-compatible server")

    # Nothing is ever created server-side, so there is never anything to delete.
    return GeminiRest(post=post, delete=lambda path: {}, upload=upload)


def _openai_compat_client(endpoint: Endpoint, timeout: float) -> httpx.Client:
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    return httpx.Client(headers=headers, timeout=timeout)


def _sse_chunks(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":  # how a Chat Completions stream ends; Gemini just closes
            return
        yield json.loads(payload)
