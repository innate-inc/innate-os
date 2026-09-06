# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Base proxy client for innate-os.

Provides both sync and async HTTP helpers that route requests through
the Innate service proxy at ``{proxy_url}/v1/services/{service}/{endpoint}``.

Authentication is handled via :class:`auth_client.AuthProvider` when an
OIDC issuer URL is available; otherwise the service key is sent directly
as a Bearer token.

Usage::

    from innate_proxy import ProxyClient

    proxy = ProxyClient(config={"cartesia_voice_id": "..."})
    proxy.cartesia.tts.bytes_stream(...)
    conn = proxy.elevenlabs.realtime.connect_sync(...)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from auth_client import AuthProvider
from auth_client.httpx_auth import InnateBearerAuth
from dotenv import load_dotenv

from innate_proxy.public_demo import demo_proxy_url, public_demo_enabled

if not public_demo_enabled():
    load_dotenv()

logger = logging.getLogger(__name__)


class ProxyClient:
    """Authenticated HTTP client for the Innate service proxy.

    Credentials come from constructor args or environment variables.
    An optional ``config`` dict carries application-level settings
    (voice IDs, model names, etc.) that adapters can read.
    """

    def __init__(
        self,
        proxy_url: str | None = None,
        innate_service_key: str | None = None,
        auth_issuer_url: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._public_demo = public_demo_enabled()
        if self._public_demo:
            if innate_service_key or auth_issuer_url or proxy_url:
                raise RuntimeError("Public simulator proxy configuration must come from its credential-free relay")
            raw_url = demo_proxy_url()
        else:
            raw_url = (proxy_url or os.getenv("INNATE_PROXY_URL", "https://proxy-v1.svc.innate.bot")).rstrip("/")
        if raw_url and not raw_url.startswith(("http://", "https://")):
            raw_url = f"https://{raw_url}"
        self.proxy_url: str = raw_url

        self._service_key: str = "" if self._public_demo else innate_service_key or os.getenv("INNATE_SERVICE_KEY", "")
        self.config: dict[str, Any] = config or {}

        issuer_url = auth_issuer_url or os.getenv("INNATE_AUTH_URL", "https://auth-v1.svc.innate.bot")
        if issuer_url and self._service_key:
            self._auth: AuthProvider | None = AuthProvider(
                issuer_url=issuer_url,
                service_key=self._service_key,
            )
            self._httpx_auth: InnateBearerAuth | None = InnateBearerAuth(self._auth)
        else:
            self._auth = None
            self._httpx_auth = None

        self._sync_client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None
        self._cartesia: Any = None
        self._elevenlabs: Any = None

    # -- Availability ---------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if proxy credentials are configured."""
        return bool(self.proxy_url and (self._public_demo or self._service_key))

    # -- Token helpers --------------------------------------------------------

    @property
    def token(self) -> str:
        """Current bearer token (JWT from OIDC or raw service key)."""
        if self._auth is not None:
            return self._auth.token
        return self._service_key

    # -- Sync HTTP ------------------------------------------------------------

    def get_sync_client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(
                timeout=60.0,
                auth=self._httpx_auth,
                headers={"User-Agent": "innate-robot"},
            )
        return self._sync_client

    def request_stream(
        self,
        service_name: str,
        endpoint: str,
        method: str = "POST",
        json: dict[str, Any] | None = None,
        data: bytes | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        timeout: float | None = None,
    ):
        """Return a context manager that yields an ``httpx.Response`` with streaming.

        Usage::

            with proxy.request_stream("cartesia", "/tts/bytes", json=body) as resp:
                for chunk in resp.iter_bytes():
                    ...
        """
        if not self.is_available():
            raise RuntimeError(
                "ProxyClient is not configured. "
                "Set INNATE_PROXY_URL and INNATE_SERVICE_KEY "
                "(in environment or .env file)."
            )

        url = f"{self.proxy_url}/v1/services/{service_name}/{endpoint.lstrip('/')}"

        kwargs: dict[str, Any] = {"method": method, "url": url, "params": params}
        if json is not None:
            kwargs["json"] = json
        elif data is not None:
            kwargs["content"] = data
        if files is not None:
            kwargs["files"] = files
        if form is not None:
            kwargs["data"] = form  # form fields: multipart beside files, urlencoded alone
        if timeout is not None:
            kwargs["timeout"] = timeout

        # Auth (incl. 401 retry) is handled by InnateBearerAuth on the client
        return self.get_sync_client().stream(**kwargs)

    # -- Async HTTP -----------------------------------------------------------

    def get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=60.0,
                auth=self._httpx_auth,
                headers={"User-Agent": "innate-robot"},
            )
        return self._async_client

    async def request_async(
        self,
        service_name: str,
        endpoint: str,
        method: str = "POST",
        json: dict[str, Any] | None = None,
        data: bytes | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Make an asynchronous request through the proxy."""
        if not self.is_available():
            raise RuntimeError(
                "ProxyClient is not configured. "
                "Set INNATE_PROXY_URL and INNATE_SERVICE_KEY "
                "(in environment or .env file)."
            )
        client = self.get_async_client()
        url = f"{self.proxy_url}/v1/services/{service_name}/{endpoint.lstrip('/')}"

        kwargs: dict[str, Any] = {"method": method, "url": url, "params": params}
        if json is not None:
            kwargs["json"] = json
        elif data is not None:
            kwargs["content"] = data

        # Auth (incl. 401 retry) is handled by InnateBearerAuth on the client
        response = await client.request(**kwargs)
        response.raise_for_status()
        return response

    # -- Service adapters -----------------------------------------------------

    @property
    def innate_service_key(self) -> str:
        """The raw service key (for adapters that need it directly)."""
        return self._service_key

    @property
    def cartesia(self) -> Any:
        """Lazy Cartesia TTS adapter."""
        if self._cartesia is None:
            from innate_proxy.adapters.cartesia import ProxyCartesiaClient

            self._cartesia = ProxyCartesiaClient(self)
        return self._cartesia

    @property
    def elevenlabs(self) -> Any:
        """Lazy ElevenLabs adapter (Scribe realtime STT)."""
        if self._elevenlabs is None:
            from innate_proxy.adapters.elevenlabs import ProxyElevenLabsClient

            self._elevenlabs = ProxyElevenLabsClient(self, auth=self._auth)
        return self._elevenlabs

    # -- Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    async def close_async(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def __enter__(self) -> ProxyClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    async def __aenter__(self) -> ProxyClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close_async()
