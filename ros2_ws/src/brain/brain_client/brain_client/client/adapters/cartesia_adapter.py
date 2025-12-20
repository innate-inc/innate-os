"""Cartesia TTS adapter for proxy client."""

import logging
from typing import Dict, Any, Iterator

logger = logging.getLogger(__name__)


class ProxyCartesiaClient:
    """
    Cartesia TTS client that proxies through the innate service proxy.
    
    This maintains the same interface as the Cartesia SDK for drop-in replacement.
    """
    
    def __init__(self, parent):
        """
        Initialize Cartesia proxy client.
        
        Args:
            parent: Parent ProxyClient instance
        """
        self._parent = parent
    
    class TTS:
        """Text-to-speech API."""
        
        def __init__(self, parent):
            """Initialize TTS API with reference to parent ProxyClient."""
            self._parent = parent
        
        def bytes(
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
            body = {
                "model_id": model_id,
                "transcript": transcript,
                "voice": voice,
                "output_format": output_format,
            }
            
            # Make request through parent proxy
            response = self._parent.request(
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
        return self.TTS(self._parent)
    
    def close(self):
        """Close the client."""
        self._parent.close()
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

