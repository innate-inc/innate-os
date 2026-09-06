# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Exercise real httpx requests and the TTS handler without provider credentials."""

import json
import threading
import traceback
from types import SimpleNamespace

import httpx
import pytest

from brain_client.core.config import _PARAM_DEFAULTS
from brain_client.transport import cartesia
from brain_client.transport.tts import TTSHandler
from innate_proxy import ProxyClient

CANARY = "controlled-private-credential-canary"
VOICE = {"mode": "id", "id": _PARAM_DEFAULTS["cartesia_voice_id"]}
FORMAT = {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 44100}


@pytest.fixture(autouse=True)
def private_test_environment(monkeypatch):
    monkeypatch.delenv("INNATE_PUBLIC_DEMO", raising=False)
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)


class AudioChunks(httpx.SyncByteStream):
    def __init__(self, fail=False):
        self.closed = False
        self.fail = fail

    def __iter__(self):
        yield b"audio-first"
        if self.fail:
            raise httpx.ReadError(CANARY)
        yield b"audio-second"

    def close(self):
        self.closed = True


def direct_factory(monkeypatch, respond):
    """Replace only the network; keep httpx's request/stream/client behavior."""
    client_type = httpx.Client
    clients = []

    def create_client(**kwargs):
        client = client_type(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(cartesia.httpx, "Client", create_client)
    return clients


def proxy_client(respond):
    proxy = ProxyClient(proxy_url="https://proxy.test", innate_service_key=CANARY)
    # A controlled authenticated client replaces only OIDC/network access. The
    # real proxy adapter still builds and routes every Cartesia request.
    proxy._sync_client = httpx.Client(
        headers={"Authorization": f"Bearer {CANARY}"}, transport=httpx.MockTransport(respond)
    )
    return proxy


def handler_for(transport):
    # Run the actual synchronous speech path without starting queue threads or
    # publishing ROS messages. No physical audio device is involved.
    handler = TTSHandler.__new__(TTSHandler)
    handler._tts = transport
    handler.backend = "test-backend"
    handler.voice_id = _PARAM_DEFAULTS["cartesia_voice_id"]
    handler._simulator_mode = True
    handler.tts_audio_pub = object()
    handler.play_lock = threading.Lock()
    handler.is_playing = False
    handler._closing = threading.Event()
    messages = []
    audio = []
    handler.logger = SimpleNamespace(**{level: messages.append for level in ("info", "debug", "error")})
    handler._publish_tts_status = lambda _status: None
    handler._publish_audio = audio.append
    return handler, messages, audio


def test_direct_requests_forward_default_voice_and_reuse_then_close_client(monkeypatch):
    requests = []
    streams = []

    def respond(request):
        requests.append(request)
        stream = AudioChunks()
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    clients = direct_factory(monkeypatch, respond)
    assert cartesia.pick_tts(None) == (None, cartesia.TtsBackend.UNCONFIGURED)
    monkeypatch.setenv("CARTESIA_API_KEY", CANARY)
    transport, backend = cartesia.pick_tts(None)
    assert backend == cartesia.TtsBackend.DIRECT
    assert transport is not None
    handler, _messages, audio = handler_for(transport)
    try:
        transport.warmup()
        assert handler.speak_text("controlled transcript")
        assert audio == [b"audio-firstaudio-second"]
        assert list(handler._stream_tts_bytes("speaker transcript", VOICE, for_speaker=True)) == [
            b"audio-first",
            b"audio-second",
        ]
        assert len(clients) == 1
        assert [request.method for request in requests] == ["HEAD", "POST", "POST"]
        assert all(str(request.url) == cartesia.DIRECT_URL for request in requests)
        assert all(request.headers["Authorization"] == f"Bearer {CANARY}" for request in requests)
        assert all(request.headers["Cartesia-Version"] == cartesia.DIRECT_API_VERSION for request in requests)
        assert json.loads(requests[1].content) == {
            "model_id": "sonic-3.5",
            "transcript": "controlled transcript",
            "voice": VOICE,
            "output_format": FORMAT,
        }
        speaker = json.loads(requests[2].content)
        assert speaker["voice"] == VOICE
        assert speaker["output_format"] == {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}
        assert speaker["generation_config"] == {"speed": 1.5}
        assert all(stream.closed for stream in streams)
        assert not clients[0].is_closed
    finally:
        transport.close()
    assert clients[0].is_closed


@pytest.mark.parametrize("public_demo", ["", "1"])
def test_proxy_precedes_direct_even_in_public_demo_and_retains_shared_client(monkeypatch, public_demo):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=AudioChunks())

    monkeypatch.setenv("CARTESIA_API_KEY", CANARY)
    monkeypatch.setenv("INNATE_PUBLIC_DEMO", public_demo)
    proxy = proxy_client(respond)
    client = proxy.get_sync_client()

    def forbidden_direct(_key):
        pytest.fail("A configured proxy must never fall back to a direct key")

    monkeypatch.setattr(cartesia, "direct_tts", forbidden_direct)
    try:
        transport, backend = cartesia.pick_tts(proxy)
        assert transport is not None
        assert backend == cartesia.TtsBackend.PROXY
        transport.warmup()
        assert list(transport.stream("sonic-3.5", "hello", VOICE, FORMAT)) == [b"audio-first", b"audio-second"]
        assert [str(request.url) for request in requests] == [
            "https://proxy.test",
            "https://proxy.test/v1/services/cartesia/tts/bytes",
        ]
        assert json.loads(requests[1].content)["voice"] == VOICE
        transport.close()
        assert not client.is_closed
    finally:
        proxy.close()
    assert client.is_closed


@pytest.mark.parametrize("route", ["direct", "proxy"])
@pytest.mark.parametrize("failure", ["unauthorized", "warmup_http", "partial_stream", "warmup_exception"])
def test_failures_expose_only_status_or_generic_error_without_failover(monkeypatch, route, failure):
    requests = []
    streams = []

    def respond(request):
        requests.append(request)
        if failure == "warmup_exception":
            raise RuntimeError(CANARY)
        if failure == "partial_stream":
            stream = AudioChunks(fail=True)
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        return httpx.Response(401, text=CANARY, headers={"X-Provider-Details": CANARY})

    monkeypatch.setenv("CARTESIA_API_KEY", CANARY)
    proxy = None
    if route == "direct":
        clients = direct_factory(monkeypatch, respond)
    else:
        proxy = proxy_client(respond)

        def forbidden_direct(_key):
            pytest.fail("Provider errors must never trigger direct-key failover")

        monkeypatch.setattr(cartesia, "direct_tts", forbidden_direct)
    transport, _backend = cartesia.pick_tts(proxy)
    assert transport is not None
    handler, messages, _audio = handler_for(transport)
    expected = f"cartesia {route}: " + ("HTTP 401" if failure in {"unauthorized", "warmup_http"} else "request failed")
    try:
        with pytest.raises(RuntimeError) as error:
            if failure.startswith("warmup"):
                transport.warmup()
            else:
                list(transport.stream("sonic-3.5", "hello", VOICE, FORMAT))
        assert str(error.value) == expected
        assert CANARY not in "".join(traceback.format_exception(error.value))
        if failure.startswith("warmup"):
            handler._warmup_connection()
        else:
            assert not handler.speak_text("controlled transcript")
        assert any(expected in message for message in messages)
        assert all(CANARY not in message for message in messages)
        assert len(requests) == 2
        assert all(stream.closed for stream in streams)
    finally:
        transport.close()
        if proxy is not None:
            proxy.close()
        else:
            assert clients[0].is_closed


@pytest.mark.parametrize("flag", ["1", " true ", "YES"])
def test_public_demo_blocks_selection_construction_and_existing_direct_calls(monkeypatch, flag):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b"audio")

    clients = direct_factory(monkeypatch, respond)
    transport = cartesia.direct_tts(CANARY)
    monkeypatch.setenv("CARTESIA_API_KEY", CANARY)
    monkeypatch.setenv("INNATE_PUBLIC_DEMO", flag)
    try:
        assert cartesia.pick_tts(None) == (None, cartesia.TtsBackend.UNCONFIGURED)
        with pytest.raises(RuntimeError, match="disabled in public demo"):
            cartesia.direct_tts(CANARY)
        with pytest.raises(RuntimeError, match="disabled in public demo"):
            list(transport.stream("sonic-3.5", "hello", VOICE, FORMAT))
        with pytest.raises(RuntimeError, match="disabled in public demo"):
            transport.warmup()
        assert requests == []
        assert len(clients) == 1
    finally:
        transport.close()
    assert clients[0].is_closed
