# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Wire behavior of the batch STT transcribers (no ROS, no network): the
per-call transcribe timeout, Gemini's empty-candidates shape, decorated
NO_SPEECH replies, and the proxy client's form-only body."""

import json

import httpx
import pytest

from brain_client.brain.llm.gemini.transport import GeminiRest, proxy_rest
from brain_client.inputs.batch_stt import (
    ELEVENLABS_PROXY_ENDPOINT,
    NO_SPEECH,
    TRANSCRIBE_TIMEOUT_SECS,
    elevenlabs_proxy_transcriber,
    gemini_transcriber,
    pcm_to_wav,
)

# A real container: the ElevenLabs transcriber now parses it to build the
# raw-PCM upload form.
WAV = pcm_to_wav(b"\x00\x00" * 240, 24_000)

GEMINI_MODEL = "gemini-3.6-flash"


def gemini_rest(response: dict, calls: list | None = None) -> GeminiRest:
    def post(path, body, timeout=None):
        if calls is not None:
            calls.append((path, body, timeout))
        return response

    return GeminiRest(post=post, delete=lambda path: {}, upload=lambda path, data, mime: {})


def gemini_reply(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


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


def test_gemini_post_carries_the_transcribe_timeout():
    calls = []
    assert gemini_transcriber(gemini_rest(gemini_reply("hello robot"), calls), GEMINI_MODEL, "en")(WAV) == "hello robot"
    path, _, timeout = calls[0]
    assert path == f"/v1beta/models/{GEMINI_MODEL}:generateContent"
    assert timeout == TRANSCRIBE_TIMEOUT_SECS


def test_proxy_rest_threads_a_per_call_timeout_to_the_wire():
    proxy = FakeProxy()
    proxy_rest(proxy).post("/v1beta/models/m:generateContent", {}, timeout=12.5)
    assert proxy.calls[0][2]["timeout"] == 12.5


def test_proxy_rest_defaults_to_the_client_timeout():
    proxy = FakeProxy()
    proxy_rest(proxy).post("/v1beta/models/m:generateContent", {})
    assert proxy.calls[0][2]["timeout"] is None


def test_elevenlabs_proxy_passes_the_transcribe_timeout():
    proxy = FakeProxy(payload=json.dumps({"text": "hi"}).encode())
    assert elevenlabs_proxy_transcriber(proxy, "scribe_v2", "en")(WAV) == "hi"
    _, endpoint, kwargs = proxy.calls[0]
    assert endpoint == ELEVENLABS_PROXY_ENDPOINT
    assert kwargs["timeout"] == TRANSCRIBE_TIMEOUT_SECS


# ---------- gemini response shapes ----------


def test_gemini_empty_candidates():
    for response in ({}, {"candidates": []}):
        assert gemini_transcriber(gemini_rest(response), GEMINI_MODEL, "en")(WAV) == ""


def test_no_speech_survives_model_decoration():
    for decorated in (NO_SPEECH, "NO_SPEECH.", '"NO_SPEECH"', "'NO_SPEECH'", ' "NO_SPEECH". ', "NO_SPEECH!"):
        assert gemini_transcriber(gemini_rest(gemini_reply(decorated)), GEMINI_MODEL, "en")(WAV) == ""


def test_no_speech_inside_longer_text_is_a_real_transcript():
    for text in ("NO_SPEECH is what he said", "she whispered NO_SPEECH"):
        assert gemini_transcriber(gemini_rest(gemini_reply(text)), GEMINI_MODEL, "en")(WAV) == text


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
