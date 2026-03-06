"""Innate proxy client library.

Provides :class:`ProxyClient` for authenticated HTTP requests through the
Innate service proxy, plus drop-in adapter classes for Cartesia TTS and
OpenAI (Realtime WebSocket).

Usage::

    from innate_proxy import ProxyClient, ProxyCartesiaClient, ProxyOpenAIClient
"""

from innate_proxy.client import ProxyClient
from innate_proxy.adapters.cartesia import ProxyCartesiaClient
from innate_proxy.adapters.openai import ProxyOpenAIClient, SyncRealtimeConnection

__all__: list[str] = [
    "ProxyClient",
    "ProxyCartesiaClient",
    "ProxyOpenAIClient",
    "SyncRealtimeConnection",
]
