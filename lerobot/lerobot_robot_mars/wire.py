# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Client side of the MARS bridge protocol.

The bridge (``manipulation/lerobot_bridge.py`` in innate-os) binds two ZMQ sockets:

- PULL on ``port_actions``: clients PUSH one JSON object per message. ``{"_hb": 1}`` is a
  heartbeat; anything with the six ``jointN.pos`` keys is a command (``x.vel`` and
  ``theta.vel`` optional, default 0).
- PUB on ``port_observations``: one frame per message, ``<topic> <json header>\n<payload>``.
  Topic ``state`` has no payload; topic ``obs`` carries the JPEGs of the cameras in ``_cams``
  concatenated, their lengths in ``_sizes``. Single frames let the subscriber conflate, so a
  client always reads the newest observation, never a backlog.

The header holds ``jointN.pos`` (measured, rad), ``cmd.jointN.pos`` and ``cmd.x.vel`` /
``cmd.theta.vel`` (last commanded), ``seq``, ``t``, ``busy``. The bridge only streams while it
has heard from a client within the last couple of seconds, so every client heartbeats.
"""

from __future__ import annotations

import json
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
COMMAND_PREFIX = "cmd."
HEARTBEAT_S = 0.5

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
        if not sub.poll(timeout_ms, zmq.POLLIN):
            return None
        raw: bytes | None = None
        while True:
            try:
                raw = sub.recv(zmq.NOBLOCK)
            except zmq.Again:
                break
        return parse_message(raw) if raw is not None else None

    def send(self, payload: dict[str, float]) -> None:
        self._send(payload)

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
