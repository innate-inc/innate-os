# Innate Service Proxy Client Library

Client library for connecting to the Innate Service Proxy from innate-os robots.

## Overview

This library provides drop-in replacements for direct API calls to external services (Cartesia, OpenAI, etc.). Instead of storing API keys on robots, robots authenticate with a single `innate_service_key` and the proxy handles API key management centrally.

## Installation

Copy the `client/` directory to your robot's codebase:

```bash
# On robot
cp -r /path/to/innate_service_proxy/client /path/to/innate-os/
```

## Configuration

Set environment variables in your robot's `.env` file:

```bash
INNATE_PROXY_URL=https://your-proxy-url.run.app
INNATE_SERVICE_KEY=your-robot-token-here
```

The client library automatically reads these environment variables.

## API Overview

### Proxy Endpoint Structure

All requests go through the proxy at:
```
{proxy_url}/v1/services/{service_name}/{endpoint_path}
```

**Example:**
- Cartesia TTS: `POST /v1/services/cartesia/tts/bytes`
- OpenAI Chat: `POST /v1/services/openai/v1/chat/completions`

### Authentication

The client automatically handles authentication:
- **Application Token**: Sent in `X-Innate-Token` header (from `INNATE_SERVICE_KEY`)
- **Cloud Run IAM**: Not required - the service is publicly accessible. The client may attempt to add IAM tokens if available, but this is optional and not needed for normal operation.

## Service Adapters

### Cartesia TTS

**Before (Direct API):**
```python
from cartesia import Cartesia

client = Cartesia(api_key=os.getenv("CARTESIA_API_KEY"))
response = client.tts.bytes(
    model_id="sonic-3",
    transcript="Hello, world!",
    voice={"mode": "id", "id": "voice-id"},
    output_format={
        "container": "wav",
        "encoding": "pcm_s16le",
        "sample_rate": 44100
    }
)
# response is bytes
```

**After (Proxy):**
```python
from client.adapters.cartesia_adapter import ProxyCartesiaClient

client = ProxyCartesiaClient()  # Uses INNATE_PROXY_URL and INNATE_SERVICE_KEY from env
response = await client.tts.bytes(
    model_id="sonic-3",
    transcript="Hello, world!",
    voice={"mode": "id", "id": "voice-id"},
    output_format={
        "container": "wav",
        "encoding": "pcm_s16le",
        "sample_rate": 44100
    }
)
# response is bytes (same as before!)
```

**Key Changes:**
- Import `ProxyCartesiaClient` instead of `Cartesia`
- Use `await` (async function)
- No API key needed (handled by proxy)
- Same return type and interface

### OpenAI Chat Completions

**Before (Direct API):**
```python
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=False
)
# response is dict with 'choices' key
```

**After (Proxy):**
```python
from client.adapters.openai_adapter import ProxyOpenAIClient

client = ProxyOpenAIClient()  # Uses env vars automatically
response = await client.chat.completions(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=False
)
# response is dict with 'choices' key (same as before!)
```

**Streaming:**
```python
# Before
response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)
for chunk in response:
    print(chunk.choices[0].delta.content)

# After
response = await client.chat.completions(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)
async for chunk in response:
    print(chunk['choices'][0]['delta']['content'])
```

### OpenAI Realtime (WebSocket)

**Before (Direct API):**
```python
from openai import RealtimeClient

wss_url = f"wss://api.openai.com/v1/realtime?model={model}"
headers = [
    f"Authorization: Bearer {os.getenv('OPENAI_API_KEY')}",
    "OpenAI-Beta: realtime=v1",
]
client = RealtimeClient(wss_url, headers, ...)
```

**After (Proxy):**
```python
from client.adapters.openai_adapter import ProxyOpenAIClient

client = ProxyOpenAIClient()

async def on_message(ws, message):
    # Handle message
    pass

ws = await client.realtime.connect(
    model="gpt-4o-realtime-preview",
    on_message=on_message
)
```

## Adapter Structure

Each service has its own adapter class that mirrors the original SDK interface:

### Cartesia Adapter

```python
class ProxyCartesiaClient:
    def __init__(self, proxy_url=None, innate_service_key=None):
        # Uses env vars if not provided
    
    @property
    def tts(self) -> TTS:
        """Text-to-speech API"""
    
    class TTS:
        async def bytes(self, model_id, transcript, voice, output_format) -> bytes:
            """Generate speech audio bytes"""
```

### OpenAI Adapter

```python
class ProxyOpenAIClient:
    def __init__(self, proxy_url=None, innate_service_key=None):
        # Uses env vars if not provided
    
    @property
    def chat(self) -> Chat:
        """Chat Completions API"""
    
    @property
    def realtime(self) -> Realtime:
        """Realtime WebSocket API"""
    
    class Chat:
        async def completions(self, model, messages, stream=False, **kwargs):
            """Create chat completion"""
    
    class Realtime:
        async def connect(self, model, on_message=None):
            """Connect to Realtime API via WebSocket"""
```

## Base Client

The `ProxyClient` class handles all HTTP communication:

```python
from client.proxy_client import ProxyClient

client = ProxyClient(
    proxy_url="https://proxy-url.run.app",
    innate_service_key="your-token"
)

response = await client.request(
    service_name="cartesia",
    endpoint="/tts/bytes",
    method="POST",
    json={"model_id": "sonic-3", ...}
)
```

**You typically don't use this directly** - use the service adapters instead.

## Error Handling

The client raises standard HTTP exceptions:

```python
from httpx import HTTPStatusError

try:
    response = await client.tts.bytes(...)
except HTTPStatusError as e:
    if e.response.status_code == 401:
        print("Authentication failed - check INNATE_SERVICE_KEY")
    elif e.response.status_code == 404:
        print("Service or endpoint not found")
    else:
        print(f"Error: {e}")
```

## Migration Examples

### Example 1: Cartesia TTS in ROS2 Node

**Before:**
```python
# tts_handler.py
from cartesia import Cartesia
import os

class TTSHandler:
    def __init__(self, api_key: str, voice_id: str):
        self.api_key = api_key
        self.voice_id = voice_id
        if self.api_key and self.api_key.strip():
            self.client = Cartesia(api_key=self.api_key)
        else:
            self.client = None
    
    def speak_text(self, text: str):
        if not self.client:
            return False
        
        # Generate speech using Cartesia
        response = self.client.tts.bytes(
            model_id="sonic-3",
            transcript=text,
            voice={"mode": "id", "id": self.voice_id},
            output_format={
                "container": "wav",
                "encoding": "pcm_s16le",
                "sample_rate": 44100
            }
        )
        
        # Save to temp file and play with aplay
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(response)
            temp_filename = f.name
        
        import subprocess
        subprocess.run(["aplay", temp_filename])
        os.unlink(temp_filename)
        return True
```

**After:**
```python
# tts_handler.py
from client.adapters.cartesia_adapter import ProxyCartesiaClient
import os
import asyncio
import tempfile
import subprocess

class TTSHandler:
    def __init__(self, voice_id: str):
        # No API key needed! Uses INNATE_PROXY_URL and INNATE_SERVICE_KEY from env
        self.voice_id = voice_id
        try:
            self.client = ProxyCartesiaClient()
            self.available = True
        except Exception as e:
            self.client = None
            self.available = False
    
    def is_available(self) -> bool:
        return self.available
    
    async def speak_text_async(self, text: str):
        if not self.available:
            return False
        
        # Generate speech using proxy (same API!)
        response = await self.client.tts.bytes(
            model_id="sonic-3",
            transcript=text,
            voice={"mode": "id", "id": self.voice_id},
            output_format={
                "container": "wav",
                "encoding": "pcm_s16le",
                "sample_rate": 44100
            }
        )
        
        # Save to temp file and play (same as before)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(response)
            temp_filename = f.name
        
        subprocess.run(["aplay", temp_filename])
        os.unlink(temp_filename)
        return True
    
    def speak_text(self, text: str):
        # Wrapper for sync code (if needed)
        return asyncio.run(self.speak_text_async(text))
```

**Key Changes:**
- Removed `api_key` parameter (uses `INNATE_SERVICE_KEY` env var instead)
- Changed to async function (`speak_text_async`)
- Same return type (`bytes`) and same interface
- No changes needed to audio playback logic

### Example 2: OpenAI Realtime WebSocket

**Before:**
```python
# micro_input.py
import websockets
import os

api_key = os.getenv("OPENAI_API_KEY")
model = "gpt-4o-realtime-preview"
wss_url = f"wss://api.openai.com/v1/realtime?model={model}"

headers = [
    f"Authorization: Bearer {api_key}",
    "OpenAI-Beta: realtime=v1",
]

ws = websockets.connect(wss_url, extra_headers=headers)

async def on_message(ws, message):
    data = json.loads(message)
    # Handle message
    pass

async for message in ws:
    await on_message(ws, message)
```

**After:**
```python
# micro_input.py
from client.adapters.openai_adapter import ProxyOpenAIClient

client = ProxyOpenAIClient()  # No API key needed!

async def on_message(ws, message):
    data = json.loads(message)
    # Handle message (same as before)
    pass

# Connect via proxy WebSocket
ws = await client.realtime.connect(
    model="gpt-4o-realtime-preview",
    on_message=on_message
)

async for message in ws:
    await on_message(ws, message)
```

**Key Changes:**
- No API key needed (uses `INNATE_SERVICE_KEY` env var)
- WebSocket URL automatically points to proxy
- Same message handling logic

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `INNATE_PROXY_URL` | Yes | Proxy service URL (e.g., `https://proxy-url.run.app`) |
| `INNATE_SERVICE_KEY` | Yes | Robot authentication token |
| `GOOGLE_APPLICATION_CREDENTIALS` | Optional | Service account key path (for private deployments) |

## Troubleshooting

### "proxy_url must be provided"
- Set `INNATE_PROXY_URL` environment variable
- Or pass `proxy_url` parameter to adapter constructor

### "innate_service_key must be provided"
- Set `INNATE_SERVICE_KEY` environment variable
- Or pass `innate_service_key` parameter to adapter constructor
- Verify token is valid in BigQuery `user_management` table

### 401 Unauthorized
- Check `INNATE_SERVICE_KEY` is correct
- Verify token exists and `is_active = TRUE` in BigQuery
- Check token hasn't expired (`token_expires_at`)

### 404 Not Found
- Verify service name matches `services.yaml` configuration
- Check endpoint path is correct
- Ensure proxy service is deployed and running

### Connection Errors
- Verify `INNATE_PROXY_URL` is correct
- Check network connectivity to proxy
- Verify proxy service is running (check `/health` endpoint)

## API Reference

### ProxyClient

Base client for making requests to the proxy.

```python
class ProxyClient:
    def __init__(self, proxy_url=None, innate_service_key=None):
        """
        Initialize proxy client.
        
        Args:
            proxy_url: Proxy service URL (defaults to INNATE_PROXY_URL env var)
            innate_service_key: Authentication token (defaults to INNATE_SERVICE_KEY env var)
        """
    
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
        Make request to proxy service.
        
        Args:
            service_name: Service name (e.g., 'cartesia', 'openai')
            endpoint: Endpoint path (e.g., '/tts/bytes')
            method: HTTP method (GET, POST, etc.)
            json: JSON body (for POST/PUT)
            data: Raw bytes body
            params: Query parameters
            stream: Whether to stream response
        
        Returns:
            httpx.Response object
        """
```

### ProxyCartesiaClient

Cartesia TTS client adapter.

```python
class ProxyCartesiaClient:
    def __init__(self, proxy_url=None, innate_service_key=None):
        """Initialize Cartesia proxy client."""
    
    @property
    def tts(self) -> TTS:
        """Get TTS API."""
    
    class TTS:
        async def bytes(
            self,
            model_id: str,
            transcript: str,
            voice: Dict[str, Any],
            output_format: Dict[str, Any],
        ) -> bytes:
            """
            Generate speech audio bytes.
            
            Args:
                model_id: Cartesia model ID (e.g., 'sonic-3')
                transcript: Text to convert to speech
                voice: Voice configuration dict
                output_format: Output format configuration dict
            
            Returns:
                bytes: Audio file bytes
            """
```

### ProxyOpenAIClient

OpenAI client adapter (Chat and Realtime).

```python
class ProxyOpenAIClient:
    def __init__(self, proxy_url=None, innate_service_key=None):
        """Initialize OpenAI proxy client."""
    
    @property
    def chat(self) -> Chat:
        """Get Chat Completions API."""
    
    @property
    def realtime(self) -> Realtime:
        """Get Realtime WebSocket API."""
    
    class Chat:
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
                messages: List of message dicts
                stream: Whether to stream response
                **kwargs: Additional OpenAI API parameters
            
            Returns:
                Dict if stream=False, AsyncIterator if stream=True
            """
    
    class Realtime:
        async def connect(
            self,
            model: str,
            on_message: Optional[Callable] = None,
        ) -> WebSocket:
            """
            Connect to Realtime API via WebSocket.
            
            Args:
                model: Model name (e.g., 'gpt-4o-realtime-preview')
                on_message: Optional callback for incoming messages
            
            Returns:
                WebSocket connection object
            """
```

## Support

For issues or questions:
1. Check [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) in the main repository
2. Verify environment variables are set correctly
3. Check proxy service logs for errors
4. Contact infrastructure team for token/authentication issues

