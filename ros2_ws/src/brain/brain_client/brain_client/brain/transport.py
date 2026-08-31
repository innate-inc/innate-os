# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""How the brain reaches Gemini: the Innate proxy (managed) or GEMINI_API_KEY (dev).

The proxy holds the upstream key and passes native Gemini calls — the turn
stream and the memory search's blocking generate / context-cache management —
through untouched (the robot authenticates with its service key); the direct
path talks to ``generativelanguage.googleapis.com``. Both speak the same wire
format — a transport only moves payloads and never interprets them.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

PROXY_SERVICE = "gemini"
# Overridable so the brain can be pointed at anything that speaks this wire
# format -- a local stand-in, a record/replay proxy, another provider behind a
# translator. A transport only moves payloads and never interprets them, so the
# base URL is the whole of what has to change; without this, swapping the model
# means editing the brain.
DIRECT_BASE_URL = os.environ.get("GEMINI_BASE_URL", "").strip() or "https://generativelanguage.googleapis.com"
STREAM_PATH = "/v1beta/models/{model}:streamGenerateContent?alt=sse"
GENERATE_PATH = "/v1beta/models/{model}:generateContent"
CACHED_CONTENTS_PATH = "/v1beta/cachedContents"
FILES_UPLOAD_PATH = "/upload/v1beta/files"

# The backend has no passthrough for this endpoint at all — callers latch the
# feature off permanently rather than retry (shared by the cache and files tiers).
UNSUPPORTED_ENDPOINT_STATUSES = (404, 405, 501)

# A pooled connection the far end has already closed fails before it sends any
# response at all. Expiring ours first turns that race into a reconnect, and one
# retry covers the case where it happens anyway -- see sim/bench/FINDINGS.md
# (patch_stream_retry).
_KEEPALIVE_EXPIRY_S = 30.0
_STREAM_ATTEMPTS = 2
_RETRYABLE_CONNECT = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
)


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
    """Which way the brain reaches Gemini (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    DIRECT = "gemini-direct"
    UNCONFIGURED = "unconfigured"


def pick_transport(proxy: ProxyClient | None) -> tuple[Transport | None, Backend]:
    """The way to reach Gemini: the Innate proxy (managed) or GEMINI_API_KEY (dev).

    sim/launcher/config.py:resolve_brain_backend predicts this choice from the
    host (it cannot import this module) to label the dashboard; change the
    precedence here and change it there.
    """
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
            # Same tap as the direct path. Without it, moving to the proxy
            # silently zeroes the benchmark's cost and token figures -- a wrong
            # number reported confidently, rather than an error.
            yield from _meter_stream(model, _sse_chunks(resp.iter_lines()))

    return stream


# --- usage metering (sim/bench/FINDINGS.md, patch_usage_meter) ----------------
# Token counts come from the API response, so a cost estimate is measured
# rather than guessed -- but the metering is best effort in every direction: a
# failed write is swallowed so the brain is unaffected, which means a total is
# a floor, not a guaranteed-complete sum.
_USAGE_LOG = os.environ.get("GEMINI_USAGE_LOG", "/root/innate-os/workspace/gemini_usage.jsonl")


def _meter_usage(model: str, chunk: dict) -> None:
    usage = chunk.get("usageMetadata") if isinstance(chunk, dict) else None
    if not usage:
        return
    try:
        import json as _json
        import time as _time

        row = {
            "t": round(_time.time(), 3),
            "model": model,
            "prompt": usage.get("promptTokenCount", 0),
            "cached": usage.get("cachedContentTokenCount", 0),
            "thoughts": usage.get("thoughtsTokenCount", 0),
            "output": usage.get("candidatesTokenCount", 0),
            "total": usage.get("totalTokenCount", 0),
        }
        with open(_USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001,S110 -- metering must never break the brain
        pass


def _meter_stream(model: str, chunks: Iterator[dict]) -> Iterator[dict]:
    """Pass chunks through, writing exactly ONE usage row per stream.

    `usageMetadata` rides more than one chunk and the earlier copy is
    incomplete -- it lands before the image parts are counted, 1064 tokens per
    image. Metering per chunk therefore logged the same call twice, inflating
    call counts by ~29% and input tokens by 12.5%. See
    sim/bench/FINDINGS.md (patch_meter_dedupe).

    The write sits in a `finally` so a consumer that stops reading early is
    still charged for what it used.
    """
    latest: dict | None = None
    try:
        for chunk in chunks:
            usage = chunk.get("usageMetadata") if isinstance(chunk, dict) else None
            if usage:
                latest = usage
            yield chunk
    finally:
        if latest is not None:
            _meter_usage(model, {"usageMetadata": latest})


def direct_transport(api_key: str) -> Transport:
    """Reach Google's Gemini API directly with GEMINI_API_KEY."""
    # One client for the process: reuses the TLS connection across turns
    # instead of a fresh handshake per generate call. Single-threaded use by
    # construction (one turn at a time on the agent's worker thread). Pooled
    # connections are expired early because a connection Google closed while
    # we were idle still looks usable here, and the failure lands on the next
    # turn rather than on the idleness that caused it.
    client = httpx.Client(
        headers={"x-goog-api-key": api_key},
        timeout=90.0,
        limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_EXPIRY_S),
    )

    def stream(model: str, body: dict) -> Iterator[dict]:
        url = DIRECT_BASE_URL + STREAM_PATH.format(model=model)
        for attempt in range(_STREAM_ATTEMPTS):
            delivered = False
            try:
                with client.stream("POST", url, json=body) as resp:
                    if resp.status_code != 200:
                        resp.read()
                        raise RuntimeError(f"gemini direct: HTTP {resp.status_code}: {resp.text[:200]}")
                    for chunk in _meter_stream(model, _sse_chunks(resp.iter_lines())):
                        # Passthrough with a tap. The tap lives in _meter_stream
                        # so the call is metered once, not once per chunk that
                        # happens to carry a usage block.
                        delivered = True
                        yield chunk
                return
            except _RETRYABLE_CONNECT as error:
                # Only worth retrying before the first chunk: the server sent
                # nothing, so it processed nothing, and re-sending cannot
                # duplicate a reply the caller has already acted on. Once
                # chunks are out, a retry would replay half an answer.
                if delivered or attempt == _STREAM_ATTEMPTS - 1:
                    raise
                print(
                    f"[transport] {type(error).__name__} before any response; reconnecting once",
                    file=sys.stderr,
                    flush=True,
                )

    return stream


def pick_rest(proxy: ProxyClient | None) -> GeminiRest | None:
    """Blocking-call access to Gemini, chosen the same way as :func:`pick_transport`."""
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
    # longer timeout than the per-chunk streaming client. Same early keepalive
    # expiry as the stream client, and for a stronger reason: this one can sit
    # idle for a minute between uploads, so its pooled connection is the more
    # likely of the two to have been closed at the far end already.
    client = httpx.Client(
        headers={"x-goog-api-key": api_key},
        timeout=120.0,
        limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_EXPIRY_S),
    )

    def _rest_send(send):
        """Run a request, retrying once if the connection died before the
        server answered. Safe by the same argument as the stream retry: no
        response means nothing was processed, and a blocking call has shown
        the caller nothing yet. A real refusal arrives as a status code and
        never reaches here."""
        for attempt in range(_STREAM_ATTEMPTS):
            try:
                return send()
            except _RETRYABLE_CONNECT as error:
                if attempt == _STREAM_ATTEMPTS - 1:
                    raise
                print(
                    f"[transport] {type(error).__name__} on a REST call; reconnecting once",
                    file=sys.stderr,
                    flush=True,
                )
        raise AssertionError("unreachable")

    def request(method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        # timeout=None means "no deadline" to httpx, not the client default.
        resp = _rest_send(
            lambda: client.request(
                method,
                DIRECT_BASE_URL + path,
                json=body,
                timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
            )
        )
        if resp.status_code != 200:
            raise GeminiHttpError(resp.status_code, resp.text[:200])
        return resp.json() if resp.content else {}

    def upload(path: str, data: bytes, mime: str) -> dict:
        resp = _rest_send(
            lambda: client.post(
                DIRECT_BASE_URL + path,
                content=data,
                headers={"X-Goog-Upload-Protocol": "raw", "Content-Type": mime},
            )
        )
        if resp.status_code != 200:
            raise GeminiHttpError(resp.status_code, resp.text[:200])
        return resp.json() if resp.content else {}

    return GeminiRest(
        post=lambda path, body, timeout=None: request("POST", path, body, timeout=timeout),
        delete=lambda path: request("DELETE", path),
        upload=upload,
    )


def _sse_chunks(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line[len("data: ") :])
