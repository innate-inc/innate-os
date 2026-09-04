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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

DIRECT_URL = "https://api.cartesia.ai/tts/bytes"
DIRECT_API_VERSION = "2025-04-16"

# Alfred, the robot's launch voice, belongs to Innate's Cartesia account -- a
# request signed with anyone else's key 404s on it. Someone on their own key
# gets a public library voice instead, or the robot boots mute.
INNATE_VOICE_ID = "9fdaae0b-f885-4813-b589-3c07cf9d5fea"
PUBLIC_VOICE_ID = "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"  # "Nonfiction Man"


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


def pick_tts(proxy: ProxyClient | None) -> tuple[TtsTransport | None, TtsBackend]:
    """The way to reach Cartesia: the Innate proxy (managed) or CARTESIA_API_KEY (dev)."""
    if proxy is not None and proxy.is_available():
        return proxy_tts(proxy), TtsBackend.PROXY
    api_key = os.environ.get("CARTESIA_API_KEY", "").strip()
    if api_key:
        return direct_tts(api_key), TtsBackend.DIRECT
    return None, TtsBackend.UNCONFIGURED


def resolve_voice(voice_id: str, backend: TtsBackend) -> str:
    """The voice this backend can actually speak in (see INNATE_VOICE_ID)."""
    if backend is TtsBackend.DIRECT and voice_id == INNATE_VOICE_ID:
        return PUBLIC_VOICE_ID
    return voice_id


def proxy_tts(proxy: ProxyClient) -> TtsTransport:
    """Reach Cartesia through the Innate proxy (the proxy holds the upstream key)."""
    tts = proxy.cartesia.tts

    def warmup() -> None:
        # Any request to the proxy host warms httpx's connection pool.
        proxy.get_sync_client().head(proxy.proxy_url)

    # close is a no-op: the node owns the ProxyClient and shares it with the
    # brain and STT, so closing it here would cut those off mid-run.
    return TtsTransport(stream=tts.bytes_stream, warmup=warmup, close=lambda: None)


def direct_tts(api_key: str) -> TtsTransport:
    """Reach Cartesia directly with CARTESIA_API_KEY."""
    # One client for the process: reuses the TLS connection across utterances.
    # Single-threaded use by construction (one clip at a time on the speech worker).
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
        body: dict[str, Any] = {
            "model_id": model_id,
            "transcript": transcript,
            "voice": voice,
            "output_format": output_format,
        }
        if generation_config:
            body["generation_config"] = generation_config
        with client.stream("POST", DIRECT_URL, json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"cartesia direct: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from resp.iter_bytes()

    def warmup() -> None:
        client.head(DIRECT_URL)

    return TtsTransport(stream=stream, warmup=warmup, close=client.close)
