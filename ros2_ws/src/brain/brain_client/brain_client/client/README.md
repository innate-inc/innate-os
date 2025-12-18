# Innate Service Proxy Client Library

Client library for connecting to the Innate Service Proxy from innate-os robots.

## Overview

This library provides drop-in replacements for direct API calls to external services (Cartesia, OpenAI, etc.). Instead of storing API keys on robots, robots authenticate with a single `innate_service_key` and the proxy handles API key management centrally.

## Architecture in brain_client

The client library is integrated into the `brain_client` ROS2 package with a clear separation:

```
ros2_ws/src/brain/brain_client/brain_client/
├── client/                       # Proxy client library
│   ├── proxy_client.py          # Unified ProxyClient class
│   ├── adapters/
│   │   ├── cartesia_adapter.py  # Cartesia TTS adapter
│   │   └── openai_adapter.py    # OpenAI (Chat + Realtime) adapter
│   └── README.md                # This file
├── brain_client_node.py         # Uses proxy for TTS
├── input_manager_node.py        # Uses proxy for STT/inputs
├── input_loader.py              # Injects proxy into input devices
└── input_types.py               # InputDevice base class with proxy access
```

### Credential vs Configuration Separation

- **Credentials** (environment variables): `INNATE_PROXY_URL`, `INNATE_SERVICE_KEY`
- **Configuration** (ROS2 launch parameters): `openai_realtime_model`, `cartesia_voice_id`, etc.

This separation allows:
- Secrets to be managed securely in `.env` files
- Configuration to be changed per-launch without touching credentials

## Usage in Input Devices

Input devices (like `micro_input.py`) access proxy services via `self.proxy`. The `InputLoader` sets the logger and proxy via setter methods after instantiation:

```python
from brain_client.input_types import InputDevice

class MicroInput(InputDevice):
    def __init__(self):
        super().__init__()  # No parameters needed
        self.client = None
    
    def on_open(self):
        # Check proxy is available (set by InputLoader)
        if not self.proxy or not self.proxy.is_available():
            self.logger.error("Proxy not configured")
            return
        
        # Access config (set by InputManagerNode)
        model = self.proxy.config.get('openai_realtime_model', 'gpt-4o-realtime-preview')
        
        # Use OpenAI Realtime API via proxy
        self.client = self.proxy.openai.realtime.connect_sync(
            model=model,
            on_message=self.handle_message,
            on_open=self.on_ws_open,
            on_error=self.on_ws_error,
            on_close=self.on_ws_close,
        )
        self.client.start()
    
    def handle_message(self, ws, message):
        # Handle OpenAI messages
        data = json.loads(message)
        if data.get("type") == "conversation.item.input_audio_transcription.completed":
            transcript = data.get("transcript", "")
            self.send_data(transcript, data_type="chat_in")
```

### How the Proxy is Wired

1. **InputManagerNode** (ROS2 node):
   - Reads ROS2 parameters (`openai_realtime_model`, etc.)
   - Creates `ProxyClient(config=proxy_config)` with env credentials
   - Passes proxy to `InputLoader`

2. **InputLoader**:
   - Loads input device classes from `inputs/` directory
   - Creates instances with no constructor arguments
   - Sets `logger` and `proxy` via setter methods: `set_logger()`, `set_proxy()`

3. **InputDevice** (your code):
   - Accesses `self.proxy.openai`, `self.proxy.cartesia`
   - Accesses config via `self.proxy.config.get(...)`

```python
# In InputManagerNode.__init__():
proxy_config = {
    "openai_realtime_model": self.get_parameter("openai_realtime_model").value,
    "openai_transcribe_model": self.get_parameter("openai_transcribe_model").value,
    "cartesia_voice_id": self.get_parameter("cartesia_voice_id").value,
}
self.proxy = ProxyClient(config=proxy_config)
self.input_loader = InputLoader(self.get_logger(), proxy=self.proxy)

# InputLoader creates instances like this:
# instance = MicroInput()
# instance.set_logger(logger)
# instance.set_proxy(proxy)
```

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
from brain_client.client.proxy_client import ProxyClient

# Create ProxyClient (credentials from env vars)
proxy = ProxyClient()

# Access Cartesia via proxy.cartesia property
response = await proxy.cartesia.tts.bytes(
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
- Use `ProxyClient` and access adapters via properties (`proxy.cartesia`, `proxy.openai`)
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
from brain_client.client.proxy_client import ProxyClient

proxy = ProxyClient()  # Uses env vars automatically
response = await proxy.openai.chat.completions(
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
proxy = ProxyClient()
response = await proxy.openai.chat.completions(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)
async for chunk in response:
    print(chunk['choices'][0]['delta']['content'])
```

### OpenAI Realtime (WebSocket)

The Realtime API supports two connection modes:

#### Synchronous Connection (recommended for audio streaming)

Best for microphone input where you need low-latency audio streaming from a thread:

```python
from brain_client.client.proxy_client import ProxyClient

proxy = ProxyClient()

def on_message(ws, message):
    data = json.loads(message)
    if data.get("type") == "conversation.item.input_audio_transcription.completed":
        transcript = data.get("transcript", "")
        print(f"Transcript: {transcript}")

def on_open():
    # Configure session when WebSocket opens
    session_update = {
        "type": "session.update",
        "session": {
            "input_audio_format": "pcm16",
            "input_audio_transcription": {"model": "gpt-4o-mini-transcribe", "language": "en"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.3,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 700,
                "create_response": False,
            },
            "instructions": "Transcribe user audio only in English; do not reply.",
        },
    }
    conn.send_json(session_update)

conn = proxy.openai.realtime.connect_sync(
    model="gpt-4o-realtime-preview",
    on_message=on_message,
    on_open=on_open,
    on_error=lambda e: print(f"Error: {e}"),
    on_close=lambda: print("Closed"),
)
conn.start()

# Wait for connection
if conn.wait_until_connected(timeout=10):
    # Send audio chunks (from another thread)
    payload = {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(audio_bytes).decode("ascii"),
    }
    conn.send_json(payload)
```

**Key features of sync connection:**
- Uses `websocket-client` library for low-latency
- Non-blocking `send_json()` for audio streaming
- Thread-safe - send audio from any thread
- Callbacks run in WebSocket thread

#### Async Connection

For async/await code:

```python
from brain_client.client.proxy_client import ProxyClient

proxy = ProxyClient()

async def on_message(ws, message):
    # Handle message
    pass

ws = await proxy.openai.realtime.connect(
    model="gpt-4o-realtime-preview",
    on_message=on_message
)
```

## Adapter Structure

Each service has its own adapter class that takes a reference to the parent `ProxyClient`. Adapters are created lazily by the `ProxyClient` properties:

### Cartesia Adapter

```python
class ProxyCartesiaClient:
    def __init__(self, parent: ProxyClient):
        # Takes parent ProxyClient reference
        self._parent = parent
    
    @property
    def tts(self) -> TTS:
        """Text-to-speech API"""
    
    class TTS:
        def __init__(self, parent: ProxyClient):
            self._parent = parent
        
        async def bytes(self, model_id, transcript, voice, output_format) -> bytes:
            """Generate speech audio bytes via parent.request()"""
```

### OpenAI Adapter

```python
class ProxyOpenAIClient:
    def __init__(self, parent: ProxyClient):
        # Takes parent ProxyClient reference
        self._parent = parent
    
    @property
    def chat(self) -> Chat:
        """Chat Completions API"""
    
    @property
    def realtime(self) -> Realtime:
        """Realtime WebSocket API"""
    
    class Chat:
        def __init__(self, parent: ProxyClient):
            self._parent = parent
        
        async def completions(self, model, messages, stream=False, **kwargs):
            """Create chat completion via parent.request()"""
    
    class Realtime:
        def __init__(self, parent: ProxyClient):
            self._parent = parent
        
        def connect_sync(self, model, on_message=None, ...):
            """Connect via parent.proxy_url and parent.innate_service_key"""
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

### Example 2: OpenAI Realtime WebSocket (Microphone Input)

**Before (Direct API):**
```python
# micro_input.py
import websocket
import os

api_key = os.getenv("OPENAI_API_KEY")
model = "gpt-4o-realtime-preview"
wss_url = f"wss://api.openai.com/v1/realtime?model={model}"
headers = [
    f"Authorization: Bearer {api_key}",
    "OpenAI-Beta: realtime=v1",
]

ws = websocket.WebSocketApp(wss_url, header=headers, ...)
```

**After (Proxy via InputDevice):**
```python
# inputs/micro_input.py
from brain_client.input_types import InputDevice
import json
import base64

class MicroInput(InputDevice):
    def __init__(self):
        super().__init__()  # No parameters - logger/proxy set via setters
        self.client = None
    
    @property
    def name(self) -> str:
        return "micro"
    
    def on_open(self):
        # self.proxy and self.logger are set by InputLoader before on_open() is called
        if not self.proxy or not self.proxy.is_available():
            self.logger.error("Proxy not configured")
            return
        
        # Get config from proxy (set by InputManagerNode)
        model = self.proxy.config.get('openai_realtime_model', 'gpt-4o-realtime-preview')
        transcribe_model = self.proxy.config.get('openai_transcribe_model', 'gpt-4o-mini-transcribe')
        
        def on_message(ws, message):
            data = json.loads(message)
            if data.get("type") == "conversation.item.input_audio_transcription.completed":
                transcript = data.get("transcript", "")
                if transcript:
                    self.send_data(transcript, data_type="chat_in")
        
        def on_ws_open():
            session_update = {
                "type": "session.update",
                "session": {
                    "input_audio_format": "pcm16",
                    "input_audio_transcription": {"model": transcribe_model, "language": "en"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.3,
                        "create_response": False,
                    },
                },
            }
            self.client.send_json(session_update)
        
        # Connect via proxy - uses INNATE_PROXY_URL and INNATE_SERVICE_KEY from env
        self.client = self.proxy.openai.realtime.connect_sync(
            model=model,
            on_message=on_message,
            on_open=on_ws_open,
        )
        self.client.start()
        
        # Start audio thread...
    
    def on_close(self):
        if self.client:
            self.client.stop()
```

**Key Changes:**
- Extends `InputDevice` base class
- Proxy injected by `InputManagerNode` (no manual setup)
- Config accessed via `self.proxy.config` (no env var lookups)
- Uses `connect_sync()` for low-latency audio streaming
- Transcripts sent via `self.send_data()` callback

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

Cartesia TTS client adapter. Created by `ProxyClient.cartesia` property.

```python
class ProxyCartesiaClient:
    def __init__(self, parent: ProxyClient):
        """
        Initialize Cartesia proxy client.
        
        Args:
            parent: Parent ProxyClient instance (provides request(), close(), etc.)
        """
    
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

OpenAI client adapter (Chat and Realtime). Created by `ProxyClient.openai` property.

```python
class ProxyOpenAIClient:
    def __init__(self, parent: ProxyClient):
        """
        Initialize OpenAI proxy client.
        
        Args:
            parent: Parent ProxyClient instance (provides proxy_url, innate_service_key, request(), etc.)
        """
    
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
        def connect_sync(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[Callable] = None,
            on_open: Optional[Callable] = None,
            on_error: Optional[Callable] = None,
            on_close: Optional[Callable] = None,
        ) -> SyncRealtimeConnection:
            """
            Create synchronous WebSocket connection (best for audio streaming).
            
            Uses parent.proxy_url and parent.innate_service_key for connection.
            
            Args:
                model: Model name (e.g., 'gpt-4o-realtime-preview')
                on_message: Callback for messages: (ws, message: str) -> None
                on_open: Callback when connected: () -> None
                on_error: Callback on error: (error: str) -> None
                on_close: Callback on close: () -> None
            
            Returns:
                SyncRealtimeConnection with start(), stop(), send_json() methods
            """
        
        async def connect(
            self,
            model: str,
            on_message: Optional[Callable] = None,
        ) -> WebSocket:
            """
            Connect to Realtime API via async WebSocket.
            
            Args:
                model: Model name (e.g., 'gpt-4o-realtime-preview')
                on_message: Optional callback for incoming messages
            
            Returns:
                WebSocket connection object
            """

class SyncRealtimeConnection:
    """Synchronous WebSocket connection for audio streaming."""
    
    def start(self):
        """Start the WebSocket connection in background thread."""
    
    def stop(self):
        """Stop the WebSocket connection."""
    
    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        """Wait for connection. Returns True if connected."""
    
    def send_json(self, data: Dict[str, Any]):
        """Send JSON message (thread-safe, non-blocking)."""
```

## InputDevice Base Class

All input devices extend `InputDevice` from `brain_client.input_types`:

```python
from brain_client.client.proxy_client import ProxyClient

class InputDevice(ABC):
    """Base class for all input devices."""
    
    def __init__(self):
        """Initialize with default attributes. No parameters needed."""
        self.logger = None
        self._proxy: Optional[ProxyClient] = None
        self._data_callback: Optional[Callable] = None
        self._active: bool = False
        self._config: Dict[str, Any] = {}
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this input device."""
    
    @abstractmethod
    def on_open(self):
        """Called when device is activated. Start data collection here."""
    
    @abstractmethod
    def on_close(self):
        """Called when device is deactivated. Clean up resources."""
    
    @property
    def proxy(self) -> Optional[ProxyClient]:
        """Access to proxy services and config."""
    
    def set_proxy(self, proxy: ProxyClient):
        """Set proxy client (called by InputLoader)."""
    
    def set_logger(self, logger):
        """Set logger instance (called by InputLoader)."""
    
    def send_data(self, data: Any, data_type: str = "custom"):
        """Send data to the brain agent."""
    
    def is_active(self) -> bool:
        """Check if device is currently active."""
```

### Creating a Custom Input Device

1. Create a file in `~/innate-os/inputs/` ending in `_input.py` (e.g., `my_sensor_input.py`)
2. Define a class extending `InputDevice`:

```python
from brain_client.input_types import InputDevice

class MySensorInput(InputDevice):
    def __init__(self):
        super().__init__()  # No parameters needed
        self.sensor = None
    
    @property
    def name(self) -> str:
        return "my_sensor"
    
    def on_open(self):
        # self.logger and self.proxy are set by InputLoader before on_open()
        if self.proxy and self.proxy.is_available():
            config_value = self.proxy.config.get('my_config_key', 'default')
            # Start your input logic...
    
    def on_close(self):
        # Clean up
        pass
```

3. The `InputManagerNode` will automatically:
   - Discover your input device class (files matching `*_input.py`)
   - Create an instance with no constructor arguments
   - Set `logger` and `proxy` via setter methods
   - Call `on_open()` when activated
   - Call `on_close()` when deactivated

## Support

For issues or questions:
1. Check [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) in the main repository
2. Verify environment variables are set correctly
3. Check proxy service logs for errors
4. Contact infrastructure team for token/authentication issues

