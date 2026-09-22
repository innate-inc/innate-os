# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Client side of the MARS bridge protocol.

The protocol is described once, in the docstring of the bridge that serves it:
``ros2_ws/src/brain/manipulation/manipulation/lerobot_bridge.py``. The constants below repeat the
bridge's because the two sides run on different Pythons and share no code; ``tests/test_wire.py``
holds them equal. The bridge streams only while it hears from a client, so every client heartbeats.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import zmq
from lerobot.utils.errors import DeviceNotConnectedError

DEFAULT_PORT_ACTIONS = 5555
DEFAULT_PORT_OBSERVATIONS = 5556
TOPIC_STATE = b"state"
TOPIC_OBS = b"obs"
HEARTBEAT_KEY = "_hb"
CAMERAS_KEY = "_cams"
SIZES_KEY = "_sizes"
HEAD_KEY = "_head"
HEAD_STATE_KEY = "head.deg"
COMMAND_PREFIX = "cmd."
HEARTBEAT_S = 0.5
# The bridge streams at 30 Hz; asking this long with no answer means it is gone, not slow.
SILENT_AFTER_S = 1.0


def default_host() -> str:
    """The robot's hostname: MARS_HOST if set, else the web app's default."""
    return os.environ.get("MARS_HOST", "mars.local")


Header = dict[str, Any]
Message = tuple[Header, list[bytes]]


def parse_message(raw: bytes) -> Message | None:
    _topic, separator, rest = raw.partition(b" ")
    if not separator:
        return None
    head, separator, payload = rest.partition(b"\n")
    if not separator:
        return None
    try:
        header = json.loads(head)
    except json.JSONDecodeError:
        return None
    if not isinstance(header, dict):
        return None
    sizes = header.get(SIZES_KEY, [])
    if not isinstance(sizes, list) or sum(sizes) != len(payload):
        return header, []
    frames: list[bytes] = []
    offset = 0
    for size in sizes:
        frames.append(payload[offset : offset + size])
        offset += size
    return header, frames


class Link:
    """One client's pair of sockets to the bridge, subscribed to a single topic."""

    def __init__(self, remote_ip: str, port_actions: int, port_observations: int, topic: bytes) -> None:
        self._remote_ip = remote_ip
        self._port_actions = port_actions
        self._port_observations = port_observations
        self._topic = topic
        self._context: zmq.Context | None = None
        self._push: zmq.Socket | None = None
        self._sub: zmq.Socket | None = None
        self._last_send = 0.0
        self._asking_since: float | None = None
        self._last_ask = 0.0

    @property
    def is_open(self) -> bool:
        return self._sub is not None

    def open(self, timeout_s: float) -> Message:
        context = zmq.Context()
        push = context.socket(zmq.PUSH)
        push.setsockopt(zmq.LINGER, 0)
        push.setsockopt(zmq.SNDHWM, 8)
        push.connect(f"tcp://{self._remote_ip}:{self._port_actions}")
        sub = context.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt(zmq.CONFLATE, 1)
        sub.setsockopt(zmq.SUBSCRIBE, self._topic + b" ")
        sub.connect(f"tcp://{self._remote_ip}:{self._port_observations}")
        self._context, self._push, self._sub = context, push, sub

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.heartbeat()
            message = self.latest(timeout_ms=200)
            if message is not None:
                return message
        self.close()
        raise DeviceNotConnectedError(
            f"No '{self._topic.decode()}' messages from the MARS bridge at {self._remote_ip}:{self._port_observations} "
            f"within {timeout_s:.0f}s. Is innate-os running there with lerobot_bridge.enabled?"
        )

    def close(self) -> None:
        for socket in (self._push, self._sub):
            if socket is not None:
                socket.close()
        if self._context is not None:
            self._context.term()
        self._context = self._push = self._sub = None

    def latest(self, timeout_ms: int) -> Message | None:
        """The newest message, waiting up to *timeout_ms* for one; None if nothing arrived."""
        sub = self._require(self._sub)
        now = time.monotonic()
        # A gap between asks is a pause on this side (lerobot saving an episode), not silence from
        # the robot: the clock restarts, so only continuous asking can run it out.
        if now - self._last_ask > HEARTBEAT_S:
            self._asking_since = None
        self._last_ask = now
        if not sub.poll(timeout_ms, zmq.POLLIN):
            self._asking_since = self._asking_since or now
            return None
        self._asking_since = None
        return parse_message(sub.recv(zmq.NOBLOCK))  # CONFLATE keeps one message: the newest

    def ensure_alive(self) -> None:
        """Raise once the bridge has not answered for SILENT_AFTER_S of asking.

        Counted over continuous asking, not from the last message, so a long pause on this side
        (lerobot saving an episode) is not mistaken for a dead robot. Without it a rebooted
        robot or a dropped network reads as a frozen but healthy one, and a recording keeps writing
        the last frame with fresh timestamps.
        """
        if self._asking_since is not None and time.monotonic() - self._asking_since > SILENT_AFTER_S:
            raise DeviceNotConnectedError(
                f"The MARS bridge at {self._remote_ip} stopped answering for over {SILENT_AFTER_S:.0f} s: "
                "the robot rebooted, the network dropped, or innate-os restarted."
            )

    def send(self, payload: dict[str, float]) -> None:
        self._send(payload)

    def set_head(self, deg: float) -> None:
        self._send({HEAD_KEY: deg})

    def heartbeat(self) -> None:
        self._send({HEARTBEAT_KEY: 1})

    def heartbeat_if_due(self) -> None:
        if time.monotonic() - self._last_send >= HEARTBEAT_S:
            self.heartbeat()

    def _send(self, payload: dict[str, object]) -> None:
        push = self._require(self._push)
        try:
            push.send_string(json.dumps(payload), zmq.NOBLOCK)
        except zmq.Again:
            return  # no bridge listening yet; the next tick retries
        self._last_send = time.monotonic()

    @staticmethod
    def _require(socket: zmq.Socket | None) -> zmq.Socket:
        if socket is None:
            raise DeviceNotConnectedError("Link is not open")
        return socket
