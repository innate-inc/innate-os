# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ElevenLabs adapter for the Innate proxy client.

Provides :class:`ProxyElevenLabsClient` with a ``realtime`` WebSocket
sub-API for Scribe speech-to-text.

The adapter expects a *parent* object that exposes:

- ``parent.proxy_url``  — base URL of the proxy

An :class:`auth_client.AuthProvider` is passed separately at
construction time for WebSocket auth.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from auth_client import AuthProvider

from innate_proxy.ws import SyncRealtimeConnection

logger = logging.getLogger(__name__)

REALTIME_ENDPOINT = "v1/speech-to-text/realtime"


class ProxyElevenLabsClient:
    """ElevenLabs client that routes through the Innate service proxy.

    Unlike the OpenAI Realtime API, a Scribe session is configured entirely
    through query parameters at connect time — there is no session.update
    frame, so changing any of these means reconnecting.
    """

    def __init__(self, parent: Any, auth: AuthProvider | None = None) -> None:
        self._parent = parent
        self._auth = auth

    # -- helpers --------------------------------------------------------------

    def _get_proxy_url(self) -> str:
        return getattr(self._parent, "proxy_url", "")

    # -- Realtime -------------------------------------------------------------

    class Realtime:
        def __init__(self, elevenlabs_client: ProxyElevenLabsClient) -> None:
            self._ec = elevenlabs_client

        def _build_ws_url(self, params: dict[str, Any]) -> str:
            proxy_url = self._ec._get_proxy_url()
            ws_url = proxy_url.replace("https://", "wss://").replace("http://", "ws://")
            # Scribe takes array parameters (keyterms) as repeated keys, and
            # spells booleans lowercase.
            pairs: list[tuple[str, Any]] = []
            for key, value in params.items():
                if value is None:
                    continue
                for item in value if isinstance(value, (list, tuple)) else [value]:
                    pairs.append((key, str(item).lower() if isinstance(item, bool) else item))
            return f"{ws_url}/v1/services/elevenlabs/{REALTIME_ENDPOINT}?{urlencode(pairs)}"

        def connect_sync(
            self,
            model_id: str = "scribe_v2_realtime",
            audio_format: str = "pcm_24000",
            language_code: str | None = None,
            commit_strategy: str = "manual",
            vad_threshold: float | None = None,
            vad_silence_threshold_secs: float | None = None,
            min_speech_duration_ms: int | None = None,
            min_silence_duration_ms: int | None = None,
            keyterms: list[str] | None = None,
            no_verbatim: bool | None = None,
            filter_background_audio: bool | None = None,
            on_message: Callable | None = None,
            on_open: Callable | None = None,
            on_error: Callable | None = None,
            on_close: Callable | None = None,
        ) -> SyncRealtimeConnection:
            """Return a :class:`SyncRealtimeConnection` (call ``.start()`` to connect).

            The endpointing params (``vad_*``, ``min_*``) only bite under
            ``commit_strategy="vad"``, where Scribe cuts utterances itself;
            under ``"manual"`` the caller owns the commit and Scribe ignores them.
            """
            ws_url = self._build_ws_url(
                {
                    "model_id": model_id,
                    "audio_format": audio_format,
                    "language_code": language_code,
                    "commit_strategy": commit_strategy,
                    "vad_threshold": vad_threshold,
                    "vad_silence_threshold_secs": vad_silence_threshold_secs,
                    "min_speech_duration_ms": min_speech_duration_ms,
                    "min_silence_duration_ms": min_silence_duration_ms,
                    "keyterms": keyterms or None,
                    "no_verbatim": no_verbatim,
                    "filter_background_audio": filter_background_audio,
                }
            )
            return SyncRealtimeConnection(
                auth=self._ec._auth,
                ws_url=ws_url,
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
            )

    # -- properties -----------------------------------------------------------

    @property
    def realtime(self) -> Realtime:
        return self.Realtime(self)

    async def close(self) -> None:
        await self._parent.close_async()

    async def __aenter__(self) -> ProxyElevenLabsClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
