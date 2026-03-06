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
    proxy.openai.realtime.connect_sync(...)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx
from auth_client import AuthProvider
from auth_client.httpx_auth import InnateBearerAuth
from dotenv import load_dotenv

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
        proxy_url: Optional[str] = None,
        innate_service_key: Optional[str] = None,
        auth_issuer_url: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        raw_url = (
            proxy_url or os.getenv("INNATE_PROXY_URL", "https://proxy-v1.innate.bot")
        ).rstrip("/")
        if raw_url and not raw_url.startswith(("http://", "https://")):
            raw_url = f"https://{raw_url}"
        self.proxy_url: str = raw_url

        self._service_key: str = innate_service_key or os.getenv(
            "INNATE_SERVICE_KEY", ""
        )
        self.config: Dict[str, Any] = config or {}

        issuer_url = auth_issuer_url or os.getenv(
            "INNATE_AUTH_URL", "https://auth-v1.innate.bot"
        )
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
        self._openai: Any = None

    # -- Availability ---------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if proxy credentials are configured."""
        return bool(self.proxy_url and self._service_key)

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
        json: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
        params: Optional[Dict[str, Any]] = None,
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

        kwargs: Dict[str, Any] = {"method": method, "url": url, "params": params}
        if json is not None:
            kwargs["json"] = json
        elif data is not None:
            kwargs["content"] = data

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
        json: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
        params: Optional[Dict[str, Any]] = None,
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

        kwargs: Dict[str, Any] = {"method": method, "url": url, "params": params}
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
    def openai(self) -> Any:
        """Lazy OpenAI adapter (Chat + Realtime)."""
        if self._openai is None:
            from innate_proxy.adapters.openai import ProxyOpenAIClient

            self._openai = ProxyOpenAIClient(self)
        return self._openai

    # -- Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    async def close_async(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def __enter__(self) -> "ProxyClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    async def __aenter__(self) -> "ProxyClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close_async()
