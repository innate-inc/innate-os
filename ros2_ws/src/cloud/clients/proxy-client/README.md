# Innate Proxy Client

Routes API calls (Cartesia, OpenAI, etc.) through the Innate service proxy.
Robots authenticate with a service key; the proxy manages upstream API keys.

## Quick start

```python
from innate_proxy import ProxyClient

proxy = ProxyClient(config={"cartesia_voice_id": "..."})

# TTS (streaming)
for chunk in proxy.cartesia.tts.bytes_stream(
    model_id="sonic-3",
    transcript="Hello!",
    voice={"mode": "id", "id": proxy.config["cartesia_voice_id"]},
    output_format={"container": "wav", "encoding": "pcm_s16le", "sample_rate": 44100},
):
    process_audio(chunk)

# Chat completions (async)
resp = await proxy.openai.chat.completions(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello!"}],
)

# Realtime WebSocket (sync, for audio streaming)
conn = proxy.openai.realtime.connect_sync(
    model="gpt-4o-realtime-preview",
    on_message=on_message,
    on_open=on_open,
)
conn.start()
conn.wait_until_connected()
conn.send_json({"type": "input_audio_buffer.append", "audio": "..."})
conn.stop()
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `INNATE_PROXY_URL` | Yes | Proxy URL (e.g. `https://proxy.innate.bot`) |
| `INNATE_SERVICE_KEY` | Yes | Robot service key |
| `INNATE_AUTH_URL` | For OIDC | Auth URL for service-key → JWT exchange |

## Demos

Interactive demos that prove the proxy pipeline works end-to-end.
Run from the proxy-client directory (after `colcon build && source install/setup.bash`):

### Cartesia TTS

Synthesises text and plays the WAV through `aplay`/`paplay`/`ffplay`:

```bash
cd ros2_ws/src/cloud/clients/proxy-client
python -m demos.cartesia_tts "Hello from the robot!"
python -m demos.cartesia_tts   # uses default text
```

### OpenAI Chat

One-shot question or interactive chat loop:

```bash
cd ros2_ws/src/cloud/clients/proxy-client
python -m demos.openai_chat "What is the capital of France?"
python -m demos.openai_chat   # interactive mode, Ctrl-C to quit
```

Set `OPENAI_MODEL` to override the default (`gpt-4o-mini`).

## Architecture

All requests go through:
```
{proxy_url}/v1/services/{service_name}/{endpoint}
```

### ProxyClient

The base client handles HTTP + auth. Credentials from env or constructor.

```python
from innate_proxy import ProxyClient

proxy = ProxyClient()                          # creds from env
proxy = ProxyClient(config={"voice_id": "…"})  # with app config

proxy.is_available()   # True if creds are set
proxy.proxy_url        # resolved URL
proxy.token            # current JWT (or raw key)
proxy.config           # your app config dict
```

Low-level request (you usually don't need this — use the adapters):

```python
with proxy.request_stream("cartesia", "/tts/bytes", json=body) as resp:
    for chunk in resp.iter_bytes(): ...
resp = await proxy.request_async("openai", "/v1/chat/completions", json={...})
```

### Cartesia adapter

```python
proxy.cartesia.tts.bytes_stream(model_id, transcript, voice, output_format) -> Iterator[bytes]
```

### OpenAI adapter

```python
# Async chat completions
resp = await proxy.openai.chat.completions(model, messages, stream=False)

# Sync realtime WebSocket
conn = proxy.openai.realtime.connect_sync(model, on_message, on_open, on_error, on_close)
conn.start() / conn.stop() / conn.send_json(data) / conn.wait_until_connected()

# Async realtime WebSocket
ws = await proxy.openai.realtime.connect(model, on_message)
```

## Error handling

```python
from httpx import HTTPStatusError

try:
    for chunk in proxy.cartesia.tts.bytes_stream(...):
        process_audio(chunk)
except HTTPStatusError as e:
    if e.response.status_code == 401:
        print("Auth failed — check INNATE_SERVICE_KEY")
```

JWT renewal on 401 is automatic when using OIDC auth.
