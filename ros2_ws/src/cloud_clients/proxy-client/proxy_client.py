"""Base proxy client for innate-os."""

import os
import logging
from typing import Optional, Dict, Any
import httpx

from auth_client import AuthProvider
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class ProxyClient:
    """Base client for connecting to the proxy service.

    When ``auth_issuer_url`` is provided (or ``INNATE_AUTH_URL`` is set),
    the ``innate_service_key`` is treated as a service key and exchanged
    for a JWT via OIDC discovery.  The JWT is automatically renewed when
    the server responds with 401 + ``WWW-Authenticate: Bearer error="invalid_token"``
    (RFC 6750).

    If no issuer URL is provided, ``innate_service_key`` is sent directly
    as a Bearer token (useful for local testing with raw JWTs).
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        innate_service_key: Optional[str] = None,
        auth_issuer_url: Optional[str] = None,
    ):
        """
        Initialize proxy client.

        Args:
            proxy_url: Proxy service URL (defaults to INNATE_PROXY_URL env var)
            innate_service_key: Service key for JWT exchange (defaults to INNATE_SERVICE_KEY env var)
            auth_issuer_url: innate-auth issuer URL for OIDC (defaults to INNATE_AUTH_URL env var)
        """
        proxy_url_raw = (proxy_url or os.getenv("INNATE_PROXY_URL", "")).rstrip("/")
        if not proxy_url_raw:
            raise ValueError(
                "proxy_url must be provided or set INNATE_PROXY_URL environment variable"
            )

        # Ensure URL has a scheme
        if not proxy_url_raw.startswith(("http://", "https://")):
            proxy_url_raw = f"https://{proxy_url_raw}"

        self.proxy_url = proxy_url_raw

        service_key = innate_service_key or os.getenv("INNATE_SERVICE_KEY", "")
        if not service_key:
            raise ValueError(
                "innate_service_key must be provided or set INNATE_SERVICE_KEY environment variable"
            )

        # Set up auth: OIDC (service-key → JWT) or plain bearer token
        issuer_url = auth_issuer_url or os.getenv("INNATE_AUTH_URL", "")
        if issuer_url:
            self._auth = AuthProvider(
                issuer_url=issuer_url,
                service_key=service_key,
            )
        else:
            self._auth = None

        self._service_key = service_key
        self._client = httpx.AsyncClient(timeout=60.0)
        self._update_auth_header()

    def _update_auth_header(self) -> None:
        """Refresh the client Authorization header."""
        if self._auth is not None:
            self._client.headers["Authorization"] = f"Bearer {self._auth.token}"
        else:
            self._client.headers["Authorization"] = f"Bearer {self._service_key}"

    @staticmethod
    def _is_token_expired(response: httpx.Response) -> bool:
        """Check for RFC 6750 invalid_token on a 401 response."""
        if response.status_code != 401:
            return False
        www_auth = response.headers.get("WWW-Authenticate", "")
        return "invalid_token" in www_auth

    async def request(
        self,
        service_name: str,
        endpoint: str,
        method: str = "POST",
        json: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
        params: Optional[Dict[str, Any]] = None,
        stream: bool = False,
    ) -> httpx.Response:
        """
        Make a request through the proxy with automatic JWT renewal.

        Args:
            service_name: Name of the service (e.g., 'cartesia', 'openai')
            endpoint: API endpoint path
            method: HTTP method
            json: JSON body
            data: Raw body data
            params: Query parameters
            stream: Whether to stream the response

        Returns:
            httpx.Response
        """
        url = f"{self.proxy_url}/v1/services/{service_name}/{endpoint.lstrip('/')}"

        kwargs: Dict[str, Any] = {"method": method, "url": url, "params": params}
        if json is not None:
            kwargs["json"] = json
        elif data is not None:
            kwargs["content"] = data

        try:
            response = await self._client.request(**kwargs)

            # Auto-renew JWT on RFC 6750 invalid_token
            if self._auth is not None and self._is_token_expired(response):
                logger.info("JWT expired — renewing and retrying %s %s", method, url)
                self._auth.token_needs_renewal = True
                self._update_auth_header()
                response = await self._client.request(**kwargs)

            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Proxy request failed: {e.response.status_code} - {e.response.text}"
            )
            raise
        except Exception as e:
            logger.error(f"Proxy request error: {e}")
            raise

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
