# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Native OpenAI Responses events through the service proxy or an owner-local key.

The caller owns conversation/model policy. This transport never falls back to a
different account after an API failure, and never includes upstream error bodies
in exceptions: providers can echo credentials in an authentication error.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import httpx

from brain_client.brain.transport import Transport

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

RESPONSES_PATH = "/v1/responses"
DIRECT_URL = "https://api.openai.com" + RESPONSES_PATH


def pick_openai_transport(proxy: ProxyClient | None) -> tuple[Transport | None, str]:
    """Proxy first; direct keys are available only outside the public demo."""
    if proxy is not None and proxy.is_available():
        return proxy_transport(proxy), "innate-proxy"
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key and os.environ.get("INNATE_PUBLIC_DEMO", "").strip().lower() not in {"1", "true", "yes"}:
        return direct_transport(key), "openai-direct"
    return None, "unconfigured"


class OpenAITransportError(RuntimeError):
    """Sanitized failure safe to show in robot chat and logs."""


def _events(response: httpx.Response) -> Iterator[dict]:
    if response.status_code != 200:
        # Even a truncated authentication response can contain the whole key.
        raise OpenAITransportError(
            f"OpenAI request failed (HTTP {response.status_code}); check credentials and model access"
        )
    completed = False
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        # The managed proxy wraps upstream SSE event-name lines in data fields.
        # JSON data lines are still separate; these labels carry no response body.
        if data.startswith("event:"):
            continue
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except ValueError:
            raise OpenAITransportError("OpenAI returned an invalid stream event") from None
        if not isinstance(event, dict):
            raise OpenAITransportError("OpenAI returned an invalid stream event")
        if event.get("type") in {"error", "response.failed", "response.incomplete"}:
            raise OpenAITransportError("OpenAI response did not complete; check model access and request limits")
        completed = completed or event.get("type") == "response.completed"
        yield event
    if not completed:
        raise OpenAITransportError("OpenAI stream ended before completion")


def proxy_transport(proxy: ProxyClient) -> Transport:
    def stream(model: str, body: dict) -> Iterator[dict]:
        try:
            with proxy.request_stream("openai", RESPONSES_PATH, json={**body, "model": model, "stream": True}) as resp:
                yield from _events(resp)
        except OpenAITransportError:
            raise
        except Exception:
            raise OpenAITransportError("OpenAI proxy connection failed") from None

    return stream


def direct_transport(api_key: str) -> Transport:
    def stream(model: str, body: dict) -> Iterator[dict]:
        if os.environ.get("INNATE_PUBLIC_DEMO", "").strip().lower() in {"1", "true", "yes"}:
            raise OpenAITransportError("Direct OpenAI access is disabled in the public simulator")
        try:
            # Per-call ownership also closes sockets when a consumer closes the
            # generator early. No client pool survives a replaced brain context.
            with httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=90.0) as client:
                with client.stream("POST", DIRECT_URL, json={**body, "model": model, "stream": True}) as resp:
                    yield from _events(resp)
        except OpenAITransportError:
            raise
        except Exception:
            raise OpenAITransportError("OpenAI direct connection failed") from None

    return stream
