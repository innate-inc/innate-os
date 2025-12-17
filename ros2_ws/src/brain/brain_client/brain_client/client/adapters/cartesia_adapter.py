"""Cartesia TTS adapter for proxy client."""

import logging
from typing import Optional, Dict, Any, Iterator
from brain_client.client.proxy_client import ProxyClient

logger = logging.getLogger(__name__)


class ProxyCartesiaClient:
    """
    Cartesia TTS client that proxies through the innate service proxy.
    
    This maintains the same interface as the Cartesia SDK for drop-in replacement.
    """
    
    def __init__(
        self,
        proxy_url: Optional[str] = None,
        innate_service_key: Optional[str] = None,
    ):
        """
        Initialize Cartesia proxy client.
        
        Args:
            proxy_url: Proxy service URL
            innate_service_key: Authentication token
        """
        self._proxy_client = ProxyClient(proxy_url=proxy_url, innate_service_key=innate_service_key)
    
    class TTS:
        """Text-to-speech API."""
        
        def __init__(self, proxy_client: ProxyClient):
            """Initialize TTS API."""
            self._proxy_client = proxy_client
        
        async def bytes(
            self,
            model_id: str,
            transcript: str,
            voice: Dict[str, Any],
            output_format: Dict[str, Any],
        ) -> bytes | Iterator[bytes]:
            """
            Generate speech audio bytes.
            
            Args:
                model_id: Cartesia model ID (e.g., 'sonic-3')
                transcript: Text to convert to speech
                voice: Voice configuration
                output_format: Output format configuration
                
            Returns:
                bytes or Iterator[bytes] for streaming
            """
            import asyncio
            
            body = {
                "model_id": model_id,
                "transcript": transcript,
                "voice": voice,
                "output_format": output_format,
            }
            
            # Make request through proxy
            response = await self._proxy_client.request(
                service_name="cartesia",
                endpoint="/tts/bytes",
                method="POST",
                json=body,
            )
            
            # Return bytes (streaming handled by response stream)
            return response.content
    
    @property
    def tts(self) -> TTS:
        """Get TTS API."""
        return self.TTS(self._proxy_client)
    
    async def close(self):
        """Close the client."""
        await self._proxy_client.close()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

