# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Real ROS parameter and rosbridge disclosure regression (run in the ROS image).

No provider connection or motion action is invoked. INNATE_TEST_ROSBRIDGE=1
also exercises the HTTP /ws front door and installed rws_server on loopback.
"""

import asyncio
import importlib
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

rclpy = pytest.importorskip("rclpy")
from rcl_interfaces.srv import GetParameters  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "ros2_ws/src/cloud"
for package in ("innate_uninavid", "innate_logger", "innate_training_node"):
    sys.path.insert(0, str(CLOUD / package))
for package in ("proxy-client", "auth-client", "training-client"):
    sys.path.insert(0, str(CLOUD / "clients" / package))
sys.path.insert(0, str(ROOT / "webapp/proxy"))

from innate_uninavid.node import UninavidNode  # noqa: E402

CANARY = "synthetic-ros-credential-canary"


@pytest.fixture
def ros_node(monkeypatch):
    monkeypatch.setenv("INNATE_SERVICE_KEY", CANARY)
    monkeypatch.delenv("INNATE_PUBLIC_DEMO", raising=False)
    rclpy.init()
    node = UninavidNode()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        yield node
    finally:
        executor.shutdown()
        thread.join(timeout=5)
        node.destroy_node()
        rclpy.shutdown()


def test_service_key_is_private_configuration_not_a_ros_parameter(ros_node):
    # Owner auth still receives the environment key; no external auth is invoked.
    assert ros_node._service_key == CANARY and ros_node._auth is not None
    peer = Node("credential_audit_peer")
    try:
        client = peer.create_client(GetParameters, "/uninavid_node/get_parameters")
        assert client.wait_for_service(timeout_sec=10)
        result = client.call_async(GetParameters.Request(names=["service_key"]))
        rclpy.spin_until_future_complete(peer, result, timeout_sec=10)
        values = result.result().values
        assert values == []
        normal = client.call_async(GetParameters.Request(names=["forward_speed"]))
        rclpy.spin_until_future_complete(peer, normal, timeout_sec=10)
        assert normal.result().values[0].double_value == 0.3
        assert "/brain/backend_config" not in dict(ros_node.get_topic_names_and_types())
    finally:
        peer.destroy_node()


@pytest.mark.skipif(os.environ.get("INNATE_TEST_ROSBRIDGE") != "1", reason="requires installed rws_server")
def test_public_ws_cannot_retrieve_service_key(ros_node):
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer

    saved = sys.argv
    sys.argv = sys.argv[:1]
    try:
        frontdoor = importlib.import_module("https_server")
    finally:
        sys.argv = saved
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    bridge = subprocess.Popen(
        ["ros2", "run", "rws", "rws_server", "--ros-args", "-p", f"port:={port}", "-p", "rosbridge_compatible:=true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    saved_url = frontdoor.ROSBRIDGE_URL
    frontdoor.ROSBRIDGE_URL = f"ws://127.0.0.1:{port}"

    async def request():
        async with TestClient(TestServer(frontdoor.build_app())) as client:
            # Readiness retries are bounded; only loopback is contacted.
            for _ in range(100):
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("local rws server did not start")
            async with client.ws_connect("/ws") as ws:

                async def call(name):
                    await ws.send_json(
                        {
                            "op": "call_service",
                            "id": name,
                            "service": "/uninavid_node/get_parameters",
                            "type": "rcl_interfaces/srv/GetParameters",
                            "args": {"names": [name]},
                        }
                    )
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        payload = json.loads(message.data)
                        if payload.get("id") == name and payload.get("op") == "service_response":
                            assert CANARY not in message.data
                            assert payload.get("result") is True
                            return payload["values"]["values"]
                    pytest.fail("no ROS parameter service response received")

                assert await asyncio.wait_for(call("service_key"), timeout=20) == []
                values = await asyncio.wait_for(call("forward_speed"), timeout=20)
                assert values[0]["double_value"] == 0.3

    try:
        asyncio.run(request())
    finally:
        frontdoor.ROSBRIDGE_URL = saved_url
        bridge.terminate()
        bridge.wait(timeout=10)


def test_public_demo_navigation_uses_credential_free_relay(monkeypatch):
    from types import SimpleNamespace

    import innate_uninavid.node as navigation

    from innate_proxy.public_demo import _CREDENTIAL_NAME

    for name in os.environ:
        if _CREDENTIAL_NAME.search(name.upper()):
            monkeypatch.delenv(name)
    monkeypatch.setenv("INNATE_PUBLIC_DEMO", "1")
    monkeypatch.setenv("INNATE_DEMO_PROXY_URL", "http://127.0.0.1:8081")
    rclpy.init()
    node = UninavidNode()
    try:
        assert node._auth is None and node._service_key == ""
        assert node._ws_url == "ws://127.0.0.1:8081/uninavid/ws"
        seen = []

        class CompletedClient:
            state = navigation.ClientState.COMPLETED

            def __init__(self, **kwargs):
                assert kwargs["auth_provider"] is None
                assert kwargs["url"] == node._ws_url

            def connect(self, instruction):
                seen.append(instruction)

            def disconnect(self):
                pass

        monkeypatch.setattr(navigation, "UninavidWsClient", CompletedClient)
        goal = SimpleNamespace(
            request=SimpleNamespace(instruction="look at the chair"),
            is_active=True,
            is_cancel_requested=False,
            succeed=lambda: seen.append("completed"),
        )
        assert node._execute(goal).success
        assert seen == ["look at the chair", "completed"]
    finally:
        node.destroy_node()
        rclpy.shutdown()
