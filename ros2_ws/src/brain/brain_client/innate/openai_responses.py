# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""One call to the OpenAI Responses API, over whichever route is configured.

Deliberately small. It exists so a skill can run against an owner's own
``OPENAI_API_KEY`` without the Innate proxy, and is expected to be replaced by
the brain's full provider support rather than grown. There is no client object
and no SDK: the body a caller passes is the body that goes on the wire, and the
parsed JSON comes straight back.

An upstream error body never reaches the exception — a provider can echo the
whole key back in an authentication failure — so callers get the status and
nothing else.
"""

from __future__ import annotations

import json
import os
from typing import Any

RESPONSES_PATH = "/v1/responses"
DEFAULT_BASE_URL = "https://api.openai.com"
_TRUE = {"1", "true", "yes"}


class OpenAIError(RuntimeError):
    """A failure safe to surface in robot feedback and logs."""


def _direct(key: str, base_url: str):
    """The owner's own key, straight to OpenAI or an API-compatible endpoint."""
    import httpx

    url = base_url.rstrip("/") + RESPONSES_PATH

    def call(body: dict[str, Any], timeout: float) -> dict[str, Any]:
        response = httpx.post(
            url,
            json=body,
            timeout=timeout,
            headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"},
        )
        if response.status_code != 200:
            raise OpenAIError(f"OpenAI request failed (HTTP {response.status_code}); check the key and model access")
        return response.json()

    return call


def _proxied(proxy):
    """The Innate proxy, which holds the provider credential instead."""

    def call(body: dict[str, Any], timeout: float) -> dict[str, Any]:
        with proxy.request_stream("openai", RESPONSES_PATH, method="POST", json=body, timeout=timeout) as response:
            # A streamed response carries no body until it is read; .json() on one
            # raises ResponseNotRead rather than returning anything.
            payload = response.read()
            if response.status_code != 200:
                raise OpenAIError(f"OpenAI request failed (HTTP {response.status_code}); check proxy access")
            return json.loads(payload)

    return call


def responses_api() -> tuple[Any, str]:
    """``(call, route)`` for this installation, preferring an explicit key.

    Setting ``OPENAI_API_KEY`` is a deliberate act, so it wins over a proxy that
    merely happens to be configured; the chosen route is recorded in the run's
    manifest so which account paid is never a guess. ``OPENAI_BASE_URL`` points
    the direct route at an API-compatible endpoint. A public demo may not spend
    a stray key and stays on the proxy.
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    public_demo = os.environ.get("INNATE_PUBLIC_DEMO", "").strip().lower() in _TRUE
    if key and not public_demo:
        return _direct(key, os.environ.get("OPENAI_BASE_URL", "").strip() or DEFAULT_BASE_URL), "openai-direct"
    try:
        from innate_proxy import ProxyClient
    except ImportError:
        ProxyClient = None
    proxy = ProxyClient() if ProxyClient is not None else None
    if proxy is not None and proxy.is_available():
        # This proxy path can strip the upstream encoding header while still
        # returning a gzipped body, which then fails to parse as JSON.
        proxy.get_sync_client().headers["Accept-Encoding"] = "identity"
        return _proxied(proxy), "innate-proxy"
    raise OpenAIError(
        "No route to the OpenAI Responses API: set OPENAI_API_KEY for your own account, "
        "or configure INNATE_PROXY_URL and INNATE_SERVICE_KEY for the Innate proxy"
    )
