# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Exercise real HTTP routing with a local provider double and no real keys."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from brain_client.brain import openai_transport as transport

KEY = "controlled-openai-key"
EVENTS = [
    {"type": "response.output_text.delta", "delta": "Hello"},
    {"type": "response.completed", "response": {"output": []}},
]


@pytest.fixture
def provider(monkeypatch):
    state = {"status": 200, "body": "".join(f"data: {json.dumps(e)}\n\n" for e in EVENTS), "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, dict(self.headers), body))
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(state["body"].encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(transport, "DIRECT_URL", state["url"] + "/v1/responses")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("INNATE_PUBLIC_DEMO", raising=False)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_direct_and_service_key_precedence(provider):
    stream, backend = transport.pick_openai_transport(None)
    assert backend == "openai-direct"
    body = {"input": [{"role": "user", "content": "Hello"}], "store": False}
    assert list(stream("gpt-6-astra", body)) == EVENTS
    path, headers, request = provider["requests"].pop()
    assert path == "/v1/responses"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert request == {**body, "model": "gpt-6-astra", "stream": True}
    assert "model" not in body

    class Proxy:
        def is_available(self):
            return True

        @contextmanager
        def request_stream(self, service, endpoint, **kwargs):
            assert service == "openai"
            with httpx.Client() as client:
                with client.stream(
                    "POST",
                    provider["url"] + "/proxy" + endpoint,
                    headers={"Authorization": "Bearer controlled-service-key"},
                    **kwargs,
                ) as response:
                    yield response

    stream, backend = transport.pick_openai_transport(Proxy())
    assert backend == "innate-proxy"
    assert list(stream("gpt-6-astra", body)) == EVENTS
    path, headers, _ = provider["requests"].pop()
    assert path == "/proxy/v1/responses"
    assert KEY not in str(headers)
    provider.update(status=401, body=f"invalid key {KEY}")
    with pytest.raises(transport.OpenAITransportError, match="HTTP 401") as failure:
        list(stream("gpt-6-astra", body))
    assert KEY not in str(failure.value)
    assert len(provider["requests"]) == 1  # failed service route did not try the direct account


@pytest.mark.parametrize(
    "body",
    [
        "data: invalid " + KEY + "\n\n",
        'data: {"type":"error","message":"' + KEY + '"}\n\n',
        'data: {"type":"response.failed","response":{"error":"' + KEY + '"}}\n\n',
        'data: {"type":"response.incomplete"}\n\n',
        "data: [DONE]\n\n",
        "",
    ],
)
def test_stream_failures_are_sanitized(provider, body):
    provider["body"] = body
    stream, _ = transport.pick_openai_transport(None)
    with pytest.raises(transport.OpenAITransportError) as failure:
        list(stream("gpt-6-astra", {}))
    assert KEY not in str(failure.value)


def test_missing_keys_demo_guard_and_connection_errors(provider, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    assert transport.pick_openai_transport(None) == (None, "unconfigured")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("INNATE_PUBLIC_DEMO", "1")
    assert transport.pick_openai_transport(None) == (None, "unconfigured")
    with pytest.raises(transport.OpenAITransportError, match="public simulator"):
        list(transport.direct_transport(KEY)("gpt-6-astra", {}))
    assert provider["requests"] == []
    monkeypatch.delenv("INNATE_PUBLIC_DEMO")

    class FailingProxy:
        def is_available(self):
            return True

        def request_stream(self, *_args, **_kwargs):
            raise ValueError(f"authentication failed: {KEY}")

    stream, _ = transport.pick_openai_transport(FailingProxy())
    with pytest.raises(transport.OpenAITransportError, match="proxy connection failed") as failure:
        list(stream("gpt-6-astra", {}))
    assert KEY not in str(failure.value)
    assert provider["requests"] == []


def test_proxy_wrapped_sse_event_names_are_not_json(provider):
    provider["body"] = "".join(f"data: event: {event['type']}\n\ndata: {json.dumps(event)}\n\n" for event in EVENTS)
    stream, _ = transport.pick_openai_transport(None)
    assert list(stream("gpt-6-astra", {})) == EVENTS


@pytest.mark.parametrize("value", ["true", " yes ", "TRUE"])
def test_public_demo_boolean_variants_block_direct_keys(provider, monkeypatch, value):
    monkeypatch.setenv("INNATE_PUBLIC_DEMO", value)
    assert transport.pick_openai_transport(None) == (None, "unconfigured")
    with pytest.raises(transport.OpenAITransportError, match="disabled"):
        list(transport.direct_transport(KEY)("gpt-6-astra", {}))
