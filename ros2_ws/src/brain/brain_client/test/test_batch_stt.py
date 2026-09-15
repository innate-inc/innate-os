# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Wire behavior of the batch STT transcribers (no ROS, no network): the
per-call transcribe timeout, Gemini's empty-reply shape, decorated NO_SPEECH
replies, the proxy client's form-only body, and the Http mover both vendor
paths ride."""

import json

import httpx
import pytest

from brain_client.inputs.batch_stt import (
    ELEVENLABS_PROXY_ENDPOINT,
    NO_SPEECH,
    TRANSCRIBE_TIMEOUT_SECS,
    elevenlabs_proxy_transcriber,
    gemini_transcriber,
    pcm_to_wav,
)
from brain_client.llm import Audio, Finish, LlmError, Message, Reply, Role, Text, Thinking, Usage
from brain_client.llm.http import Http
from brain_client.llm.replay import Replay

# A real container: the ElevenLabs transcriber now parses it to build the
# raw-PCM upload form.
WAV = pcm_to_wav(b"\x00\x00" * 240, 24_000)

GEMINI_MODEL = "gemini-3.6-flash"
CLIENT_TIMEOUT = 90.0


def gemini_replay(text: str | None) -> Replay:
    """A provider answering ``text`` (None: an empty reply), recording what it was asked."""
    parts = (Text(text),) if text is not None else ()
    return Replay([Reply(Message(Role.ASSISTANT, parts), Usage(), Finish.STOP)], model=GEMINI_MODEL)


def mock_http(handler) -> Http:
    """An Http whose client answers from ``handler``. Http builds its own
    client, so swapping the attribute is the only seam a test has."""
    http = Http("https://vendor.test", timeout=CLIENT_TIMEOUT)
    http._client = httpx.Client(
        base_url="https://vendor.test", timeout=CLIENT_TIMEOUT, transport=httpx.MockTransport(handler)
    )
    return http


class FakeProxyResponse:
    def __init__(self, status_code: int, payload: bytes):
        self.status_code = status_code
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeProxyResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeProxy:
    """Capturing ProxyClient stand-in — a fresh one-shot response per call, like the real stream()."""

    def __init__(self, status_code: int = 200, payload: bytes = b"{}"):
        self.calls: list[tuple[str, str, dict]] = []
        self._status_code = status_code
        self._payload = payload

    def request_stream(self, service: str, endpoint: str, **kwargs: object) -> FakeProxyResponse:
        self.calls.append((service, endpoint, kwargs))
        return FakeProxyResponse(self._status_code, self._payload)


# ---------- transcribe timeout ----------


def test_gemini_request_carries_the_audio_and_the_transcribe_timeout():
    replay = gemini_replay("hello robot")
    assert gemini_transcriber(replay, "en")(WAV) == "hello robot"
    request = replay.last
    audio, prompt = request.messages[-1].parts
    assert audio == Audio(WAV)
    assert isinstance(prompt, Text) and "Transcribe" in prompt.text
    assert request.thinking == Thinking.MINIMAL and request.temperature == 0.0
    assert replay.timeouts[-1] == TRANSCRIBE_TIMEOUT_SECS


def test_http_threads_a_per_call_timeout_to_the_wire():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    http = mock_http(handler)
    http.post_json("/v1/chat", {}, timeout=12.5)
    http.post_json("/v1/chat", {})
    assert requests[0].extensions["timeout"]["read"] == 12.5
    assert requests[1].extensions["timeout"]["read"] == CLIENT_TIMEOUT  # no per-call deadline: the client's


def test_elevenlabs_proxy_passes_the_transcribe_timeout():
    proxy = FakeProxy(payload=json.dumps({"text": "hi"}).encode())
    assert elevenlabs_proxy_transcriber(proxy, "scribe_v2", "en")(WAV) == "hi"
    _, endpoint, kwargs = proxy.calls[0]
    assert endpoint == ELEVENLABS_PROXY_ENDPOINT
    assert kwargs["timeout"] == TRANSCRIBE_TIMEOUT_SECS


# ---------- gemini response shapes ----------


def test_gemini_empty_reply_is_silence():
    assert gemini_transcriber(gemini_replay(None), "en")(WAV) == ""


def test_no_speech_survives_model_decoration():
    for decorated in (NO_SPEECH, "NO_SPEECH.", '"NO_SPEECH"', "'NO_SPEECH'", ' "NO_SPEECH". ', "NO_SPEECH!"):
        assert gemini_transcriber(gemini_replay(decorated), "en")(WAV) == ""


def test_no_speech_inside_longer_text_is_a_real_transcript():
    for text in ("NO_SPEECH is what he said", "she whispered NO_SPEECH"):
        assert gemini_transcriber(gemini_replay(text), "en")(WAV) == text


# ---------- SSE over the relay ----------


def test_sse_reads_payloads_from_a_relay_mangled_stream():
    # The Innate proxy's relay drops the blank line between events and re-wraps
    # `event:` lines as `data: event: …`; [DONE] still ends the stream.
    stream = b'data: {"n": 1}\ndata: event: foo\ndata: {"n": 2}\ndata: [DONE]\ndata: {"n": 3}\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream, headers={"content-type": "text/event-stream"})

    assert list(mock_http(handler).sse("/v1/stream", {})) == ['{"n": 1}', '{"n": 2}']


def test_sse_raises_the_vendors_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"upstream exploded")

    with pytest.raises(LlmError) as error:
        list(mock_http(handler).sse("/v1/stream", {}))
    assert error.value.status == 500 and "upstream exploded" in error.value.detail


# ---------- proxy client body encoding ----------


def test_proxy_form_without_files_sends_an_urlencoded_body():
    innate_proxy = pytest.importorskip("innate_proxy")

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    proxy = innate_proxy.ProxyClient(proxy_url="https://proxy.test", innate_service_key="test-key")
    proxy._sync_client = httpx.Client(transport=httpx.MockTransport(handler))

    with proxy.request_stream("elevenlabs", "v1/speech-to-text", form={"model_id": "scribe_v2"}) as resp:
        resp.read()

    request = requests[0]
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.read() == b"model_id=scribe_v2"
