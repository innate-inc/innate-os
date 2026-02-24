"""Cartesia TTS adapter for the Innate proxy client.

The adapter expects a *parent* object that exposes:

- ``parent.request_stream(service_name, endpoint, method, json=...)`` → context manager yielding ``httpx.Response``

This is satisfied by :class:`innate_proxy.ProxyClient`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator

logger = logging.getLogger(__name__)


class ProxyCartesiaClient:
    """Drop-in Cartesia TTS client that routes through the Innate service proxy."""

    def __init__(self, parent: Any) -> None:
        self._parent = parent

    class TTS:
        """Text-to-speech sub-API."""

        def __init__(self, parent: Any) -> None:
            self._parent = parent

        def bytes_stream(
            self,
            model_id: str,
            transcript: str,
            voice: Dict[str, Any],
            output_format: Dict[str, Any],
        ) -> Iterator[bytes]:
            """Generate speech audio as a streaming iterator of byte chunks.

            The proxy streams the Cartesia response back, so the caller
            can start piping audio to a player before the full response
            has been downloaded.
            """
            body = {
                "model_id": model_id,
                "transcript": transcript,
                "voice": voice,
                "output_format": output_format,
            }
            with self._parent.request_stream(
                service_name="cartesia",
                endpoint="/tts/bytes",
                method="POST",
                json=body,
            ) as response:
                response.raise_for_status()
                yield from response.iter_bytes()

    @property
    def tts(self) -> TTS:
        """Get the TTS sub-API."""
        return self.TTS(self._parent)

    def close(self) -> None:
        self._parent.close()

    def __enter__(self) -> "ProxyCartesiaClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
