"""
Unified Proxy Client for innate-os.

Provides access to all proxy services (Cartesia, OpenAI, etc.) with a consistent interface.
Used by both brain_client_node (TTS) and input_manager_node (STT/inputs).
"""

import os
import logging
from typing import Optional, Dict, Any

import httpx

logger = logging.getLogger(__name__)


class ProxyClient:
    """
    Unified proxy client providing access to all proxy services.
    
    Credentials (proxy_url, innate_service_key) come from environment variables.
    Configuration (voice_id, models, etc.) is passed via the config dict.
    
    Usage:
        proxy = ProxyClient(config={
            "cartesia_voice_id": "...",
            "openai_realtime_model": "gpt-4o-realtime-preview",
        })
        
        # Access services
        proxy.cartesia.tts(...)
        proxy.openai.realtime.connect_sync(...)
        
        # Access config
        voice_id = proxy.config["cartesia_voice_id"]
    """
    
    def __init__(
        self,
        proxy_url: Optional[str] = None,
        innate_service_key: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        logger=None,
    ):
        """
        Initialize unified proxy client.
        
        Args:
            proxy_url: Proxy service URL (defaults to INNATE_PROXY_URL env var)
            innate_service_key: Authentication token (defaults to INNATE_SERVICE_KEY env var)
            config: Service configuration dict (models, voice IDs, etc.)
            logger: Optional ROS logger to pass to service adapters for logging
        """
        self._proxy_url = (proxy_url or os.getenv("INNATE_PROXY_URL", "")).rstrip("/")
        self._innate_service_key = innate_service_key or os.getenv("INNATE_SERVICE_KEY", "")
        self._config = config or {}
        self._logger = logger
        
        # Lazy-initialized service adapters
        self._cartesia = None
        self._openai = None
        self._training = None
        
        # HTTP client (shared, lazy-initialized)
        self._http_client: Optional[httpx.Client] = None
        self._async_http_client: Optional[httpx.AsyncClient] = None
    
    @property
    def proxy_url(self) -> str:
        """Get the proxy URL."""
        return self._proxy_url
    
    @property
    def innate_service_key(self) -> str:
        """Get the service key."""
        return self._innate_service_key
    
    @property
    def config(self) -> Dict[str, Any]:
        """
        Service configuration (models, voice IDs, etc.)
        
        This allows input devices to access config without env var lookups:
            model = self.proxy.config.get("openai_realtime_model", "default")
        """
        return self._config
    
    def is_available(self) -> bool:
        """Check if proxy credentials are configured."""
        return bool(self._proxy_url and self._innate_service_key)
    
    @property
    def cartesia(self):
        """
        Cartesia TTS service adapter.
        
        Usage:
            await proxy.cartesia.tts.sse("Hello", voice_id="...", ...)
        """
        if self._cartesia is None:
            from brain_client.client.adapters.cartesia_adapter import ProxyCartesiaClient
            self._cartesia = ProxyCartesiaClient(self)
        return self._cartesia
    
    @property
    def openai(self):
        """
        OpenAI service adapter (realtime, completions, etc.)
        
        Usage:
            conn = proxy.openai.realtime.connect_sync(model="...", on_message=..., ...)
        """
        if self._openai is None:
            from brain_client.client.adapters.openai_adapter import ProxyOpenAIClient
            self._openai = ProxyOpenAIClient(self)
        return self._openai
    
    @property
    def training(self):
        """
        Training service adapter (upload datasets, track jobs, download models)
        
        Usage:
            async with proxy.training as client:
                jobs = await client.list_jobs()
        """
        if self._training is None:
            from brain_client.client.adapters.training_adapter import TrainingClient
            self._training = TrainingClient(self, logger=self._logger)
        return self._training
    
    def _get_headers(self) -> Dict[str, str]:
        """Build authentication headers."""
        return {
            "X-Innate-Token": self._innate_service_key,
        }
    
    def get_http_client(self) -> httpx.Client:
        """Get shared sync HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.Client(
                timeout=60.0,
                headers=self._get_headers(),
        )
        return self._http_client
    
    def get_async_http_client(self) -> httpx.AsyncClient:
        """Get shared async HTTP client."""
        if self._async_http_client is None:
            self._async_http_client = httpx.AsyncClient(
                timeout=60.0,
                headers=self._get_headers(),
            )
        return self._async_http_client
    
    def request(
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
        client = self.get_http_client()
        url = f"{self._proxy_url}/v1/services/{service_name}/{endpoint.lstrip('/')}"
        
        try:
            if json is not None:
                response = client.request(
                    method=method,
                    url=url,
                    json=json,
                    params=params,
                )
            else:
                response = client.request(
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
    
    async def request_async(
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
        Make an async request through the proxy.
        
        Args:
            service_name: Name of the service (e.g., 'cartesia', 'openai', 'training')
            endpoint: API endpoint path
            method: HTTP method
            json: JSON body
            data: Raw body data
            params: Query parameters
            stream: Whether to stream the response
            
        Returns:
            httpx.Response
        """
        client = self.get_async_http_client()
        url = f"{self._proxy_url}/v1/services/{service_name}/{endpoint.lstrip('/')}"
        
        try:
            if json is not None:
                response = await client.request(
                    method=method,
                    url=url,
                    json=json,
                    params=params,
                )
            else:
                response = await client.request(
                    method=method,
                    url=url,
                    content=data,
                    params=params,
                )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            logger.error(f"Proxy async request failed: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"Proxy async request error: {e}")
            raise
    
    def close(self):
        """Close HTTP client and clean up resources."""
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None
    
    async def close_async(self):
        """Close async HTTP client and clean up resources."""
        if self._async_http_client is not None:
            await self._async_http_client.aclose()
            self._async_http_client = None
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close_async()
