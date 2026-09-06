# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""How the robot's voice reaches Cartesia: the Innate proxy (managed) or CARTESIA_API_KEY (dev).

The proxy holds the upstream key and passes the TTS byte stream through untouched
(the robot authenticates with its service key); the direct path talks to
``api.cartesia.ai``. Precedence matches brain/transport.py — the service key wins,
because it also buys STT, which a Cartesia key does not.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

DIRECT_URL = "https://api.cartesia.ai/tts/bytes"
DIRECT_API_VERSION = "2025-04-16"


class TtsStream(Protocol):
    """(model, transcript, voice, output format) -> audio chunks as they arrive."""

    def __call__(
        self,
        model_id: str,
        transcript: str,
        voice: dict[str, Any],
        output_format: dict[str, Any],
        generation_config: dict[str, Any] | None = None,
    ) -> Iterator[bytes]: ...


@dataclass(frozen=True)
class TtsTransport:
    """A way out to Cartesia, plus the pre-warm that saves the first utterance
    the TLS handshake (~1-2s) and the teardown for whatever it opened."""

    stream: TtsStream
    warmup: Callable[[], None]
    close: Callable[[], None]


class TtsBackend(StrEnum):
    """Which way the robot's voice reaches Cartesia (surfaced in health)."""

    PROXY = "innate-proxy"
    DIRECT = "cartesia-direct"
    UNCONFIGURED = "unconfigured"


def _public_demo_enabled() -> bool:
    return os.environ.get("INNATE_PUBLIC_DEMO", "").strip().lower() in {"1", "true", "yes"}


def pick_tts(proxy: ProxyClient | None) -> tuple[TtsTransport | None, TtsBackend]:
    """The way to reach Cartesia: the Innate proxy (managed) or CARTESIA_API_KEY (dev)."""
    if proxy is not None and proxy.is_available():
        return proxy_tts(proxy), TtsBackend.PROXY
    if _public_demo_enabled():
        return None, TtsBackend.UNCONFIGURED
    api_key = os.environ.get("CARTESIA_API_KEY", "").strip()
    if api_key:
        return direct_tts(api_key), TtsBackend.DIRECT
    return None, TtsBackend.UNCONFIGURED


@contextmanager
def _safe_errors(backend: str) -> Iterator[None]:
    """Provider errors reach robot logs; expose only the route and HTTP status."""
    try:
        yield
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"cartesia {backend}: HTTP {exc.response.status_code}") from None
    except Exception:
        raise RuntimeError(f"cartesia {backend}: request failed") from None


def _require_private_session() -> None:
    if _public_demo_enabled():
        raise RuntimeError("Direct Cartesia credentials are disabled in public demo sessions")


def proxy_tts(proxy: ProxyClient) -> TtsTransport:
    """Reach Cartesia through the Innate proxy (the proxy holds the upstream key)."""
    tts = proxy.cartesia.tts

    def stream(
        model_id: str,
        transcript: str,
        voice: dict[str, Any],
        output_format: dict[str, Any],
        generation_config: dict[str, Any] | None = None,
    ) -> Iterator[bytes]:
        with _safe_errors("proxy"):
            yield from tts.bytes_stream(model_id, transcript, voice, output_format, generation_config)

    def warmup() -> None:
        # Any request to the proxy host warms httpx's connection pool.
        with _safe_errors("proxy"):
            proxy.get_sync_client().head(proxy.proxy_url).raise_for_status()

    # close is a no-op: the node owns the ProxyClient and shares it with the
    # brain and STT, so closing it here would cut those off mid-run.
    return TtsTransport(stream=stream, warmup=warmup, close=lambda: None)


def direct_tts(api_key: str) -> TtsTransport:
    """Reach Cartesia directly with CARTESIA_API_KEY."""
    _require_private_session()
    # One client for the process: reuses the TLS connection across utterances.
    # Single-threaded use by construction (one clip at a time on the speech worker).
    with _safe_errors("direct"):
        client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "Cartesia-Version": DIRECT_API_VERSION},
            timeout=60.0,
        )

    def stream(
        model_id: str,
        transcript: str,
        voice: dict[str, Any],
        output_format: dict[str, Any],
        generation_config: dict[str, Any] | None = None,
    ) -> Iterator[bytes]:
        _require_private_session()
        body: dict[str, Any] = {
            "model_id": model_id,
            "transcript": transcript,
            "voice": voice,
            "output_format": output_format,
        }
        if generation_config:
            body["generation_config"] = generation_config
        with _safe_errors("direct"), client.stream("POST", DIRECT_URL, json=body) as resp:
            if resp.status_code != 200:
                raise httpx.HTTPStatusError("TTS request failed", request=resp.request, response=resp)
            yield from resp.iter_bytes()

    def warmup() -> None:
        _require_private_session()
        with _safe_errors("direct"):
            client.head(DIRECT_URL).raise_for_status()

    def close() -> None:
        with _safe_errors("direct"):
            client.close()

    return TtsTransport(stream=stream, warmup=warmup, close=close)
