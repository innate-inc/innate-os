# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""How the brain reaches OpenAI: the Innate proxy (managed) or OPENAI_API_KEY (dev).

Both paths POST the same Responses body and read back the same SSE event
stream; a transport only moves payloads and never interprets them.

One asymmetry is worth knowing: the proxy's OpenAI adapter buffers the whole
generation before it emits, so speech through the proxy starts only once the
reply is complete, while the direct path streams sentence by sentence. Nothing
here can work around that — it is fixed in the proxy adapter.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

PROXY_SERVICE = "openai"
DIRECT_BASE_URL = "https://api.openai.com"
RESPONSES_PATH = "/v1/responses"

Transport = Callable[[dict], Iterator[dict]]
"""request body -> streamed response events (the model rides in the body)."""


def pick_transport(proxy: ProxyClient | None) -> Transport | None:
    """The way to reach OpenAI, or None when neither credential is configured."""
    if proxy is not None and proxy.is_available():
        return proxy_transport(proxy)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    return direct_transport(api_key) if api_key else None


def proxy_transport(proxy: ProxyClient) -> Transport:
    """Reach OpenAI through the Innate proxy (the proxy holds the upstream key)."""

    def stream(body: dict) -> Iterator[dict]:
        with proxy.request_stream(PROXY_SERVICE, RESPONSES_PATH, json=body) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"openai via proxy: HTTP {resp.status_code}: {resp.read()[:200]!r}")
            yield from _sse_events(resp.iter_lines())

    return stream


def direct_transport(api_key: str) -> Transport:
    """Reach OpenAI directly with OPENAI_API_KEY."""
    # One client for the process: reuses the TLS connection across turns
    # instead of a fresh handshake per generate call. Single-threaded use by
    # construction (one turn at a time on the agent's worker thread).
    client = httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=90.0)

    def stream(body: dict) -> Iterator[dict]:
        with client.stream("POST", DIRECT_BASE_URL + RESPONSES_PATH, json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"openai direct: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from _sse_events(resp.iter_lines())

    return stream


def _sse_events(lines: Iterable[str]) -> Iterator[dict]:
    """Parse the data frames of an SSE stream.

    Every event's own ``type`` is inside its JSON, so the ``event:`` lines carry
    nothing we need. Skipping data that isn't JSON is not just defensive: the
    proxy adapter re-serves upstream ``event:`` lines as ``data: event: ...``,
    and that garbling must not abort the stream.
    """
    for line in lines:
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[len("data: ") :])
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event
