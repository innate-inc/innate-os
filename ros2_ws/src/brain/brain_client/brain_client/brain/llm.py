# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Which model the brain talks to, and how it gets there.

A setting names the model as ``provider:name`` — ``google:gemini-3.6-flash``,
``anthropic:claude-sonnet-5``, ``openai:gpt-5.4-mini`` — and pydantic-ai
speaks each vendor's own wire (native Gemini, Anthropic Messages, OpenAI
Responses). A bare name infers its vendor from its prefix, so the retired
``gemini-3.6-flash`` setting still works. ``llm_base_url`` points ``openai-chat``
at any OpenAI-compatible server instead (a LAN vLLM, Ollama, NVIDIA NIM).

Route: the Innate proxy when the robot has a service key (it holds the vendor
keys and passes each API through under its own service name), else the
vendor's key from the environment. Claude reverses that order until the proxy
serves ``anthropic``. sim/launcher/config.py:resolve_brain_backend predicts the
proxy-vs-key choice from the host to label the dashboard — change the
precedence here and change it there.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx2
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from brain_client.brain.transport import GeminiRest, gemini_api_key, pick_rest
from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from auth_client.provider import AuthProvider
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from innate_proxy import ProxyClient

DEFAULT_MODEL = "google:gemini-3.6-flash"
LLM_API_KEY_ENV = "LLM_API_KEY"
TURN_TIMEOUT_SECS = 90.0

_KEY_ENV = {
    "google": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-chat": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_PROXY_SERVICE = {"google": "gemini", "openai": "openai", "openai-chat": "openai", "anthropic": "anthropic"}
_PROVIDERS = frozenset({"google", "openai", "openai-chat", "anthropic"})
_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh"})


class Backend(StrEnum):
    """Which way the brain reaches its model (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    DIRECT = "direct"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class LlmRoute:
    """A configured model with the settings every request carries, or the reason there is none."""

    spec: str  # "provider:name" as resolved
    model: Model | None
    backend: Backend
    gemini_rest: GeminiRest | None  # native REST beside a Gemini model: the explicit context cache
    settings: ModelSettings


def split_spec(spec: str, *, base_url: str = "") -> tuple[str, str]:
    """``"provider:name"`` -> (provider, name); a bare name infers its vendor."""
    spec = spec.strip()
    if ":" in spec:
        provider, name = spec.split(":", 1)
        if provider not in _PROVIDERS:
            raise ValueError(f"unknown LLM provider {provider!r} in {spec!r} (one of {sorted(_PROVIDERS)})")
        return provider, name
    if base_url:
        return "openai-chat", spec
    if spec.startswith("claude"):
        return "anthropic", spec
    if spec.startswith(("gpt", "o1", "o3", "o4")):
        return "openai", spec
    return "google", spec


def pick_model(
    spec: str,
    proxy: ProxyClient | None,
    *,
    base_url: str = "",
    thinking: str = "",
    extra_body: str = "",
    logger: RcutilsLogger | None = None,
) -> LlmRoute:
    provider, name = split_spec(spec or DEFAULT_MODEL, base_url=base_url)
    resolved = f"{provider}:{name}"
    settings = model_settings(thinking, extra_body, provider=provider)
    proxied = proxy is not None and proxy.is_available()
    key = os.environ.get(LLM_API_KEY_ENV if base_url else _KEY_ENV.get(provider, ""), "").strip()
    if provider == "google":
        key = gemini_api_key()
    rest = pick_rest(proxy) if provider == "google" else None

    if base_url:
        if not key:
            return LlmRoute(resolved, None, Backend.UNCONFIGURED, None, settings)
        model: Model = OpenAIChatModel(name, provider=OpenAIProvider(base_url=base_url, api_key=key))
        return LlmRoute(resolved, model, Backend.DIRECT, None, settings)
    if provider == "anthropic" and key:
        model = AnthropicModel(name, provider=AnthropicProvider(api_key=key))
        return LlmRoute(resolved, model, Backend.DIRECT, None, settings)
    if proxied and proxy is not None:
        return LlmRoute(resolved, _proxied_model(provider, name, proxy), Backend.PROXY, rest, settings)
    if not key:
        if logger is not None:
            logger.warn(f"[Brain] no route to {resolved}: no Innate service key and no {_KEY_ENV[provider]}")
        return LlmRoute(resolved, None, Backend.UNCONFIGURED, None, settings)
    return LlmRoute(resolved, _direct_model(provider, name, key), Backend.DIRECT, rest, settings)


def model_settings(thinking: str, extra_body: str, *, provider: str = "google") -> ModelSettings:
    """One settings dict for every vendor: keys a vendor does not know are ignored.

    ``thinking`` is pydantic-ai's one ladder ("" = the model's default); only
    Gemini has a ``minimal`` rung, so it rounds up to ``low`` elsewhere.
    Thought summaries are asked for on Gemini so the app can show them; Claude
    caches the system prompt, the tool list, and a rolling breakpoint on the
    newest turn — the same prefix discipline the history keeps for Gemini's
    and OpenAI's implicit caches.
    """
    settings: dict[str, Any] = {
        **AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
            anthropic_cache_messages=True,
            timeout=TURN_TIMEOUT_SECS,
        ),
        **GoogleModelSettings(google_thinking_config={"include_thoughts": True}),
    }
    if thinking:
        if thinking not in _THINKING_LEVELS:
            raise ValueError(f"llm_thinking must be one of {sorted(_THINKING_LEVELS)} or empty, not {thinking!r}")
        settings["thinking"] = "low" if thinking == "minimal" and provider != "google" else thinking
    if extra_body:
        settings["extra_body"] = json.loads(extra_body)
    return cast("ModelSettings", settings)


def _direct_model(provider: str, name: str, key: str) -> Model:
    if provider == "google":
        return GoogleModel(name, provider=GoogleProvider(api_key=key))
    if provider == "openai":
        return OpenAIResponsesModel(name, provider=OpenAIProvider(api_key=key))
    if provider == "openai-chat":
        return OpenAIChatModel(name, provider=OpenAIProvider(api_key=key))
    return AnthropicModel(name, provider=AnthropicProvider(api_key=key))


def _proxied_model(provider: str, name: str, proxy: ProxyClient) -> Model:
    """A vendor SDK client aimed at the proxy's service path, authenticating as the robot."""
    base = f"{proxy.proxy_url}/v1/services/{_PROXY_SERVICE[provider]}"
    auth = proxy.auth_provider
    if provider == "google":
        client = httpx2.AsyncClient(auth=_bearer2(auth), timeout=TURN_TIMEOUT_SECS)
        return GoogleModel(name, provider=GoogleProvider(api_key="innate-proxy", base_url=base, http_client=client))
    if provider == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(
            base_url=base, auth_token="innate-proxy", http_client=anthropic.DefaultAsyncHttpxClient(auth=_bearer2(auth))
        )
        return AnthropicModel(name, provider=AnthropicProvider(anthropic_client=client))
    import openai

    client = openai.AsyncOpenAI(
        base_url=f"{base}/v1",
        api_key="innate-proxy",
        http_client=openai.DefaultAsyncHttpxClient(auth=_bearer2(auth), transport=_SseRepair()),
    )
    openai_provider = OpenAIProvider(openai_client=client)
    return (
        OpenAIChatModel(name, provider=openai_provider)
        if provider == "openai-chat"
        else OpenAIResponsesModel(name, provider=openai_provider)
    )


def _bearer2(auth: AuthProvider | None) -> httpx2.Auth | None:
    return None if auth is None else _InnateBearerAuth2(auth)


class _InnateBearerAuth2(httpx2.Auth):
    """auth_client.httpx_auth.InnateBearerAuth for the httpx2 the vendor SDKs use."""

    def __init__(self, provider: AuthProvider) -> None:
        self._provider = provider

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._provider.token}"
        response = yield request
        if response.status_code == 401:
            self._provider.token_needs_renewal = True
            request.headers["Authorization"] = f"Bearer {self._provider.token}"
            yield request


class _SseRepair(httpx2.AsyncHTTPTransport):
    """Re-terminate the SSE events the proxy's OpenAI relay mangles.

    innate-cloud proxy/services/openai.py re-emits ``aiter_lines()`` as
    ``line + "\\n"``: the blank line ending every event is dropped and
    ``event:`` lines come back as ``data: event: …``, so the SDK's decoder sees
    no events at all. Delete this class once the relay forwards bytes untouched.
    """

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await super().handle_async_request(request)
        stream = response.stream
        if response.headers.get("content-type", "").startswith("text/event-stream") and isinstance(
            stream, httpx2.AsyncByteStream
        ):
            response.stream = _Reterminated(stream)
        return response


class _Reterminated(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream) -> None:
        self._inner = inner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        pending = b""
        async for chunk in self._inner:
            pending += chunk
            *lines, pending = pending.split(b"\n")  # the tail may be a line still in flight
            out = b""
            for line in lines:
                if not line:
                    continue
                if line.startswith(b"data: event: "):
                    out += line[len(b"data: ") :] + b"\n"
                elif line.startswith(b"data:"):
                    out += line + b"\n\n"
                else:
                    out += line + b"\n"
            if out:
                yield out
        if pending:
            yield pending + b"\n\n"

    async def aclose(self) -> None:
        await self._inner.aclose()
