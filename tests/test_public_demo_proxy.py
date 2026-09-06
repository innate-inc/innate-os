# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Exercise the real HTTP and realtime adapters against a loopback relay.

No provider call or credential is needed; the upstream asserts no auth is sent.
"""

import asyncio
import os
import sys
import threading
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
for package in ("proxy-client", "auth-client"):
    sys.path.insert(0, str(ROOT / "ros2_ws/src/cloud/clients" / package))
sys.path.insert(0, str(ROOT / "ros2_ws/src/mars_bot/mars_bringup"))
sys.path.insert(0, str(ROOT / "ros2_ws/src/brain/brain_client"))
sys.path.insert(0, str(ROOT / "ros2_ws/src/cloud/innate_uninavid"))

from innate_uninavid.ws_client import UninavidWsClient  # noqa: E402
from mars_bringup import config_loader  # noqa: E402

from brain_client.brain.transport import proxy_transport  # noqa: E402
from innate_proxy import ProxyClient  # noqa: E402
from innate_proxy.public_demo import _CREDENTIAL_NAME, check_runtime  # noqa: E402


@pytest.fixture
def public_env(monkeypatch, tmp_path):
    for name in os.environ:
        if _CREDENTIAL_NAME.search(name.upper()):
            monkeypatch.delenv(name)
    monkeypatch.setenv("INNATE_PUBLIC_DEMO", "1")
    monkeypatch.setenv("INNATE_DEMO_PROXY_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))


def test_public_proxy_http_and_realtime(public_env, monkeypatch):
    async def exercise():
        seen = []

        async def receive(request):
            assert "Authorization" not in request.headers
            assert "x-api-key" not in request.headers
            seen.append(request.path)
            if request.path.endswith("/realtime"):
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.send_str('{"message_type":"session_started"}')
                async for message in ws:
                    await ws.send_str(message.data)
                return ws
            if request.path == "/uninavid/ws":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                assert (await ws.receive()).data == "look at the chair"
                await ws.send_str("1,2,3")
                await ws.close()
                return ws
            if request.path.endswith("/tts/bytes"):
                assert (await request.json())["transcript"] == "Hello"
                return web.Response(body=b"synthetic-pcm")
            if request.path.endswith(":streamGenerateContent"):
                assert request.query.get("alt") == "sse"
                return web.Response(text='data: {"candidates": []}\n\n', content_type="text/event-stream")
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", receive)
        server = TestServer(app)
        await server.start_server()
        monkeypatch.setenv("INNATE_DEMO_PROXY_URL", f"http://127.0.0.1:{server.port}")
        proxy = ProxyClient()
        assert proxy.is_available() and proxy.token == proxy.innate_service_key == ""
        assert proxy._auth is None
        conn = None
        try:

            def tts():
                return b"".join(proxy.cartesia.tts.bytes_stream("sonic-3", "Hello", {}, {}))

            assert await asyncio.to_thread(tts) == b"synthetic-pcm"
            result = await proxy.request_async("gemini", "/v1beta/models/test:generateContent", json={})
            assert result.json() == {"ok": True}
            assert await asyncio.to_thread(lambda: list(proxy_transport(proxy)("test", {}))) == [{"candidates": []}]
            received = threading.Event()
            conn = proxy.elevenlabs.realtime.connect_sync(on_message=lambda _ws, _msg: received.set())
            conn.start()
            assert await asyncio.to_thread(conn.wait_until_connected, 3)
            assert await asyncio.to_thread(received.wait, 3)
            assert any(path.endswith("/realtime") for path in seen)
            navigation = UninavidWsClient(f"ws://127.0.0.1:{server.port}/uninavid/ws")
            navigation._instruction = "look at the chair"
            await asyncio.wait_for(navigation._session(), timeout=3)
            assert navigation.pop_action() == 3
        finally:
            if conn:
                await asyncio.to_thread(conn.stop)
            proxy.close()
            await proxy.close_async()
            await server.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "name",
    [
        "INNATE_SERVICE_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "DATABASE_URL",
    ],
)
def test_public_credentials_fail_without_echo(public_env, monkeypatch, name):
    canary = "synthetic-sensitive-canary"
    monkeypatch.setenv(name, canary)
    with pytest.raises(RuntimeError) as failure:
        ProxyClient()
    assert canary not in str(failure.value)


def test_public_environment_urls_cannot_carry_credentials(public_env, monkeypatch):
    monkeypatch.setenv("CUSTOM_ENDPOINT", "https://user:synthetic-url-canary@127.0.0.1")
    with pytest.raises(RuntimeError) as failure:
        ProxyClient()
    assert "synthetic-url-canary" not in str(failure.value)


def test_public_proxy_requires_explicit_relay_and_refuses_args(public_env, monkeypatch):
    with pytest.raises(RuntimeError):
        ProxyClient(innate_service_key="synthetic-sensitive-canary")
    monkeypatch.delenv("INNATE_DEMO_PROXY_URL")
    with pytest.raises(RuntimeError):
        ProxyClient()
    monkeypatch.setenv("INNATE_DEMO_PROXY_URL", "http://user:synthetic-sensitive-canary@127.0.0.1")
    with pytest.raises(RuntimeError) as failure:
        ProxyClient()
    assert "synthetic-sensitive-canary" not in str(failure.value)


def test_public_startup_and_restart_refuse_owner_files(public_env, monkeypatch, tmp_path):
    owner_env = tmp_path / ".env"
    owner_env.write_text("INNATE_SERVICE_KEY=synthetic-sensitive-canary\n")
    monkeypatch.setattr(config_loader, "SYSTEM_ENV_PATH", tmp_path / "system.env")
    with pytest.raises(RuntimeError):
        check_runtime()
    config_loader.load_env_file(owner_env)
    assert "INNATE_SERVICE_KEY" not in os.environ
    owner_env.unlink()
    check_runtime()


def test_owner_configuration_remains_available(monkeypatch, tmp_path):
    monkeypatch.delenv("INNATE_PUBLIC_DEMO", raising=False)
    owner_env = tmp_path / ".env"
    owner_env.write_text("INNATE_SERVICE_KEY=synthetic-owner-canary\n")
    monkeypatch.setattr(config_loader, "SYSTEM_ENV_PATH", tmp_path / "system.env")
    monkeypatch.setenv("INNATE_SERVICE_KEY", "")
    config_loader.load_env_file(owner_env)
    proxy = ProxyClient()
    assert proxy.is_available() and proxy.innate_service_key == "synthetic-owner-canary"
    assert proxy._auth is not None
