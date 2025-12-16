"""Base proxy client for innate-os."""

import os
import logging
from typing import Optional, Dict, Any
import httpx

# Try to import Google auth for Cloud Run IAM authentication
try:
    from google.auth import default
    from google.auth.transport.requests import Request
    _GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    _GOOGLE_AUTH_AVAILABLE = False

logger = logging.getLogger(__name__)


class ProxyClient:
    """Base client for connecting to the proxy service."""
    
    def __init__(
        self,
        proxy_url: Optional[str] = None,
        innate_service_key: Optional[str] = None,
    ):
        """
        Initialize proxy client.
        
        Args:
            proxy_url: Proxy service URL (defaults to INNATE_PROXY_URL env var)
            innate_service_key: Authentication token (defaults to INNATE_SERVICE_KEY env var)
        """
        self.proxy_url = (proxy_url or os.getenv("INNATE_PROXY_URL", "")).rstrip("/")
        self.innate_service_key = innate_service_key or os.getenv("INNATE_SERVICE_KEY", "")
        
        if not self.proxy_url:
            raise ValueError("proxy_url must be provided or set INNATE_PROXY_URL environment variable")
        
        if not self.innate_service_key:
            raise ValueError("innate_service_key must be provided or set INNATE_SERVICE_KEY environment variable")
        
        # Build headers with application token
        headers = {
            "X-Innate-Token": self.innate_service_key,  # Application authentication
        }
        
        # If Google auth is available, try to add Cloud Run IAM token
        # This is optional - if public access is enabled, we don't need it
        # Note: This uses the default credentials (service account key or gcloud auth)
        if _GOOGLE_AUTH_AVAILABLE:
            try:
                # Try using gcloud command first (most reliable for user accounts)
                import subprocess
                try:
                    identity_token = subprocess.check_output(
                        ["gcloud", "auth", "print-identity-token"],
                        stderr=subprocess.DEVNULL
                    ).decode().strip()
                    headers["Authorization"] = f"Bearer {identity_token}"
                    logger.debug("Added Cloud Run IAM identity token via gcloud")
                except Exception:
                    # Fallback: try using google.auth to get identity token
                    try:
                        credentials, _ = default()
                        request = Request()
                        credentials.refresh(request)
                        
                        from google.oauth2 import id_token
                        from google.auth.transport import requests as google_requests
                        # Request identity token for Cloud Run
                        identity_token = id_token.fetch_id_token(
                            google_requests.Request(),
                            self.proxy_url
                        )
                        headers["Authorization"] = f"Bearer {identity_token}"
                        logger.debug("Added Cloud Run IAM identity token via google.auth")
                    except Exception:
                        # If we can't get identity token, that's OK - public access may be enabled
                        logger.debug("Could not get Google identity token (public access may be enabled)")
            except Exception as e:
                # If auth fails, that's OK - public access may be enabled
                logger.debug(f"Could not get Google identity token (public access may be enabled): {e}")
        
        self._client = httpx.AsyncClient(
            timeout=60.0,
            headers=headers,
        )
    
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
        Make a request through the proxy.
        
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
        
        try:
            # Only pass json OR content, not both (httpx doesn't like both even if one is None)
            if json is not None:
                response = await self._client.request(
                    method=method,
                    url=url,
                    json=json,
                    params=params,
                )
            else:
                response = await self._client.request(
                    method=method,
                    url=url,
                    content=data,
                    params=params,
                )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            logger.error(f"Proxy request failed: {e.response.status_code} - {e.response.text}")
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

