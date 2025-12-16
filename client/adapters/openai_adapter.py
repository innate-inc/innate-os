"""OpenAI adapter for proxy client."""

import logging
import json
from typing import Optional, Dict, Any, AsyncIterator
from client.proxy_client import ProxyClient
import websockets
import asyncio

logger = logging.getLogger(__name__)


class ProxyOpenAIClient:
    """
    OpenAI client that proxies through the innate service proxy.
    
    Supports both HTTP (Chat Completions) and WebSocket (Realtime) APIs.
    """
    
    def __init__(
        self,
        proxy_url: Optional[str] = None,
        innate_service_key: Optional[str] = None,
    ):
        """
        Initialize OpenAI proxy client.
        
        Args:
            proxy_url: Proxy service URL
            innate_service_key: Authentication token
        """
        import os
        self._proxy_url = proxy_url or os.getenv("INNATE_PROXY_URL", "")
        self._innate_service_key = innate_service_key or os.getenv("INNATE_SERVICE_KEY", "")
        self._proxy_client = ProxyClient(proxy_url=proxy_url, innate_service_key=innate_service_key)
    
    class Chat:
        """Chat Completions API."""
        
        def __init__(self, proxy_client: ProxyClient):
            """Initialize Chat API."""
            self._proxy_client = proxy_client
        
        async def completions(
            self,
            model: str,
            messages: list[Dict[str, str]],
            stream: bool = False,
            **kwargs: Any,
        ) -> Dict[str, Any] | AsyncIterator[Dict[str, Any]]:
            """
            Create chat completion.
            
            Args:
                model: Model name (e.g., 'gpt-4')
                messages: List of messages
                stream: Whether to stream the response
                **kwargs: Additional OpenAI API parameters
                
            Returns:
                Response dict or async iterator for streaming
            """
            body = {
                "model": model,
                "messages": messages,
                **kwargs,
            }
            # Only include stream field if True (some APIs reject stream: false)
            if stream:
                body["stream"] = True
            
            response = await self._proxy_client.request(
                service_name="openai",
                endpoint="/v1/chat/completions",
                method="POST",
                json=body,
            )
            
            if stream:
                # Return async iterator for SSE streaming
                async def stream_response():
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]  # Remove "data: " prefix
                            if data.strip() == "[DONE]":
                                break
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                continue
                
                return stream_response()
            else:
                return response.json()
    
    class Realtime:
        """Realtime API (WebSocket)."""
        
        def __init__(self, proxy_url: str, innate_service_key: str):
            """Initialize Realtime API."""
            self._proxy_url = proxy_url.rstrip("/")
            self._innate_service_key = innate_service_key
        
        async def connect(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[callable] = None,
        ) -> websockets.WebSocketServerProtocol:
            """
            Connect to OpenAI Realtime API via proxy.
            
            Args:
                model: Model name
                on_message: Callback for received messages
                
            Returns:
                WebSocket connection
            """
            # Build WebSocket URL
            ws_url = self._proxy_url.replace("https://", "wss://").replace("http://", "ws://")
            ws_url = f"{ws_url}/v1/services/openai/v1/realtime?model={model}&token={self._innate_service_key}"
            
            # Connect to proxy WebSocket
            ws = await websockets.connect(
                ws_url,
                extra_headers={
                    "Authorization": f"Bearer {self._innate_service_key}",
                },
            )
            
            # Set up message handler
            if on_message:
                async def message_handler():
                    async for message in ws:
                        await on_message(ws, message)
                
                asyncio.create_task(message_handler())
            
            return ws
    
    @property
    def chat(self) -> Chat:
        """Get Chat API."""
        return self.Chat(self._proxy_client)
    
    @property
    def realtime(self) -> Realtime:
        """Get Realtime API."""
        return self.Realtime(self._proxy_url, self._innate_service_key)
    
    async def close(self):
        """Close the client."""
        await self._proxy_client.close()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

