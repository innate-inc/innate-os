# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Which model, over which wire, reached how — from one setting and the keys at hand.

A setting names the model as ``vendor:name`` — ``google:gemini-3.6-flash``,
``anthropic:claude-sonnet-5``, ``openai:gpt-5.4-mini`` — and each vendor has
its own wire (native Gemini, Anthropic Messages, OpenAI Responses). A bare
name infers its vendor from its prefix, so the retired ``gemini-3.6-flash``
setting still works. ``openai-chat`` with ``base_url`` points the Chat
Completions wire at any OpenAI-compatible server (a LAN vLLM, Ollama, NIM).

The way there: the Innate proxy when the robot has a service key (it holds
the vendor keys and passes each API through under its own service name),
else the vendor's key from the environment. Claude reverses that order until
the proxy serves ``anthropic``. sim/launcher/config.py:resolve_brain_backend
predicts the proxy-vs-key choice from the host to label the dashboard —
change the precedence here and change it there.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_client.common.enums import StrEnum
from brain_client.llm import anthropic, gemini, openai_chat, openai_responses
from brain_client.llm.http import Http
from brain_client.llm.provider import Adapter, Provider
from brain_client.llm.types import Json

if TYPE_CHECKING:
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from innate_proxy import ProxyClient

DEFAULT_MODEL = "google:gemini-3.6-flash"
LLM_API_KEY_ENV = "LLM_API_KEY"
TURN_TIMEOUT_SECS = 90.0


class Vendor(StrEnum):
    """The ``vendor:`` prefix of a model setting — wire-visible in settings.yaml, never renamed."""

    GOOGLE = "google"
    OPENAI = "openai"
    OPENAI_CHAT = "openai-chat"
    ANTHROPIC = "anthropic"


class Backend(StrEnum):
    """Which way the brain reaches its model (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    DIRECT = "direct"
    UNCONFIGURED = "unconfigured"


_KEY_ENV: dict[Vendor, str] = {
    Vendor.GOOGLE: "GEMINI_API_KEY",
    Vendor.OPENAI: "OPENAI_API_KEY",
    Vendor.OPENAI_CHAT: "OPENAI_API_KEY",
    Vendor.ANTHROPIC: "ANTHROPIC_API_KEY",
}
# The proxy's service path per vendor (no anthropic yet); OpenAI's adapters take a "/v1" root.
_PROXY_SERVICE: dict[Vendor, str] = {
    Vendor.GOOGLE: "gemini",
    Vendor.OPENAI: "openai/v1",
    Vendor.OPENAI_CHAT: "openai/v1",
}
_ADAPTER: dict[Vendor, Adapter] = {
    Vendor.GOOGLE: gemini.ADAPTER,
    Vendor.OPENAI: openai_responses.ADAPTER,
    Vendor.OPENAI_CHAT: openai_chat.ADAPTER,
    Vendor.ANTHROPIC: anthropic.ADAPTER,
}
_BASE_URL: dict[Vendor, str] = {
    Vendor.GOOGLE: gemini.BASE_URL,
    Vendor.OPENAI: openai_responses.BASE_URL,
    Vendor.OPENAI_CHAT: openai_chat.BASE_URL,
    Vendor.ANTHROPIC: anthropic.BASE_URL,
}


@dataclass(frozen=True)
class Llm:
    """A configured model — or, with no provider, the reason there is none."""

    spec: str  # "vendor:name" as resolved
    provider: Provider | None
    backend: Backend

    @property
    def model(self) -> str:
        return self.spec.split(":", 1)[1]


def split_spec(spec: str, *, base_url: str = "") -> tuple[Vendor, str]:
    """``"vendor:name"`` -> (vendor, name); a bare name infers its vendor."""
    spec = spec.strip()
    if ":" in spec:
        prefix, name = spec.split(":", 1)
        try:
            return Vendor(prefix), name
        except ValueError:
            raise ValueError(
                f"unknown LLM vendor {prefix!r} in {spec!r} (one of {[v.value for v in Vendor]})"
            ) from None
    if base_url:
        return Vendor.OPENAI_CHAT, spec
    if spec.startswith("claude"):
        return Vendor.ANTHROPIC, spec
    if spec.startswith(("gpt", "o1", "o3", "o4")):
        return Vendor.OPENAI, spec
    return Vendor.GOOGLE, spec


def configure(
    spec: str,
    proxy: ProxyClient | None,
    *,
    base_url: str = "",
    extra_body: str = "",
    logger: RcutilsLogger | None = None,
) -> Llm:
    vendor, name = split_spec(spec or DEFAULT_MODEL, base_url=base_url)
    resolved = f"{vendor}:{name}"
    extra: Json = json.loads(extra_body) if extra_body else {}
    adapter = _ADAPTER[vendor]
    key = vendor_key(vendor, base_url=base_url)
    proxied = proxy is not None and proxy.is_available() and vendor in _PROXY_SERVICE

    def llm(http: Http, backend: Backend) -> Llm:
        return Llm(resolved, _provider(adapter, http, name, extra), backend)

    if base_url:
        if not key:
            return Llm(resolved, None, Backend.UNCONFIGURED)
        return llm(Http(base_url.rstrip("/"), headers=_bearer(key), timeout=TURN_TIMEOUT_SECS), Backend.DIRECT)
    if vendor == Vendor.ANTHROPIC and key:
        return llm(Http(_BASE_URL[vendor], headers=vendor_headers(vendor, key)), Backend.DIRECT)
    if proxied and proxy is not None:
        return llm(proxy_http(proxy, _PROXY_SERVICE[vendor]), Backend.PROXY)
    if not key:
        if logger is not None:
            logger.warn(f"[Brain] no way to reach {resolved}: no Innate service key and no {_KEY_ENV[vendor]}")
        return Llm(resolved, None, Backend.UNCONFIGURED)
    return llm(Http(_BASE_URL[vendor], headers=vendor_headers(vendor, key)), Backend.DIRECT)


def vendor_key(vendor: Vendor, *, base_url: str = "") -> str:
    if base_url:
        return os.environ.get(LLM_API_KEY_ENV, "").strip()
    if vendor == Vendor.GOOGLE:
        return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    return os.environ.get(_KEY_ENV[vendor], "").strip()


def vendor_headers(vendor: Vendor, key: str) -> dict[str, str]:
    """Each vendor's own way of taking a key."""
    if vendor == Vendor.GOOGLE:
        return {"x-goog-api-key": key}
    if vendor == Vendor.ANTHROPIC:
        return {"x-api-key": key, "anthropic-version": anthropic.API_VERSION}
    return _bearer(key)


def proxy_http(proxy: ProxyClient, service: str) -> Http:
    """The proxy's service path, authenticating as the robot (JWT renewed on 401 by the auth flow)."""
    from auth_client.httpx_auth import InnateBearerAuth

    auth_provider = proxy.auth_provider
    base = f"{proxy.proxy_url}/v1/services/{service}"
    if auth_provider is None:
        return Http(base, headers=_bearer(proxy.token), timeout=TURN_TIMEOUT_SECS)
    return Http(base, auth=InnateBearerAuth(auth_provider), timeout=TURN_TIMEOUT_SECS)


def _provider(adapter: Adapter, http: Http, model: str, extra_body: Json) -> Provider:
    if adapter is gemini.ADAPTER:
        return gemini.GeminiProvider(adapter, http, model, extra_body)
    return Provider(adapter, http, model, extra_body)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}
