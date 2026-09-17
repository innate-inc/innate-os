# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ZMQ bridge that lets a LeRobot process observe and drive MARS without touching ROS.

ROS-free on purpose: manipulation_server owns the topics and hands this class the latest
sensor data and three publish callbacks. The client half lives in innate-os/lerobot
(``lerobot_robot_mars.wire``); the protocol is documented there and pinned by tests on both sides.

Sockets (bridge binds, clients connect):
- PULL ``port_actions``: one JSON object per message. ``{"_hb": 1}`` is a heartbeat; an object
  with the six ``jointN.pos`` keys is a command (``x.vel`` / ``theta.vel`` optional, default 0).
- PUB ``port_observations``: every message is one frame, ``<topic> <json header>\n<payload>``.
  Topic ``state`` has no payload. Topic ``obs`` carries the JPEGs of the cameras listed in
  ``_cams`` concatenated, their byte lengths in ``_sizes``. One frame per message lets a
  subscriber set ZMQ_CONFLATE and always read the newest observation instead of a backlog.

The bridge streams only while a client has been heard within ``idle_after_s``, so an idle robot
encodes nothing. Once a client has commanded the base, silence longer than ``watchdog_s`` stops it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence

import cv2
import numpy as np
import zmq

DEFAULT_PORT_ACTIONS = 5555
DEFAULT_PORT_OBSERVATIONS = 5556
TOPIC_STATE = b"state"
TOPIC_OBS = b"obs"
HEARTBEAT_KEY = "_hb"
CAMERAS_KEY = "_cams"
SIZES_KEY = "_sizes"
COMMAND_PREFIX = "cmd."

JOINTS: tuple[str, ...] = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")
JOINT_KEYS: tuple[str, ...] = tuple(f"{joint}.pos" for joint in JOINTS)
BASE_KEYS: tuple[str, ...] = ("x.vel", "theta.vel")
CAMERAS: tuple[str, ...] = ("head", "wrist")

ArmCallback = Callable[[Sequence[float]], None]
BaseCallback = Callable[[float, float], None]
StopCallback = Callable[[], None]
Log = Callable[[str], None]


class LeRobotBridge:
    def __init__(
        self,
        on_arm: ArmCallback,
        on_base: BaseCallback,
        on_stop_base: StopCallback,
        *,
        port_actions: int = DEFAULT_PORT_ACTIONS,
        port_observations: int = DEFAULT_PORT_OBSERVATIONS,
        jpeg_quality: int = 90,
        idle_after_s: float = 2.0,
        watchdog_s: float = 0.5,
        bind_address: str = "*",
        log: Log = lambda _message: None,
    ) -> None:
        self._on_arm = on_arm
        self._on_base = on_base
        self._on_stop_base = on_stop_base
        self._port_actions = port_actions
        self._port_observations = port_observations
        self._jpeg_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
        self._idle_after_s = idle_after_s
        self._watchdog_s = watchdog_s
        self._bind_address = bind_address
        self._log = log
        self._context: zmq.Context | None = None
        self._pull: zmq.Socket | None = None
        self._pub: zmq.Socket | None = None
        self._last_seen: float | None = None
        self._last_action: float | None = None
        self._base_stopped = True
        self._seq = 0
        # Set by the owner while a skill or policy is executing: commands are then ignored.
        self.commands_blocked = False

    def bind(self) -> None:
        context = zmq.Context()
        pull = context.socket(zmq.PULL)
        pull.setsockopt(zmq.LINGER, 0)
        pull.setsockopt(zmq.RCVHWM, 64)
        pull.bind(f"tcp://{self._bind_address}:{self._port_actions}")
        pub = context.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.setsockopt(zmq.SNDHWM, 8)
        pub.bind(f"tcp://{self._bind_address}:{self._port_observations}")
        self._context, self._pull, self._pub = context, pull, pub

    def close(self) -> None:
        for socket in (self._pull, self._pub):
            if socket is not None:
                socket.close()
        if self._context is not None:
            self._context.term()
        self._context = self._pull = self._pub = None

    def active(self, now: float) -> bool:
        return self._last_seen is not None and now - self._last_seen <= self._idle_after_s

    def poll(self, now: float) -> None:
        """Drain the action socket, apply the newest command, and run the base watchdog."""
        if self._pull is None:
            return
        newest: dict | None = None
        while True:
            try:
                raw = self._pull.recv(zmq.NOBLOCK)
            except zmq.Again:
                break
            self._last_seen = now
            payload = _parse(raw)
            if payload is None or HEARTBEAT_KEY in payload:
                continue
            newest = payload
        if newest is not None:
            self._apply(newest, now)
        self._watchdog(now)

    def publish(
        self,
        now: float,
        *,
        joints: Sequence[float],
        commanded: Sequence[float],
        base: tuple[float, float],
        images: Mapping[str, np.ndarray | None],
        busy: bool,
    ) -> bool:
        """Send one state message and one observation message; False when no client is listening."""
        if self._pub is None or not self.active(now):
            return False
        self._seq += 1
        header: dict[str, object] = {"seq": self._seq, "t": now, "busy": busy}
        header.update(zip(JOINT_KEYS, (float(v) for v in joints), strict=True))
        header.update(zip((COMMAND_PREFIX + k for k in JOINT_KEYS), (float(v) for v in commanded), strict=True))
        header.update(zip((COMMAND_PREFIX + k for k in BASE_KEYS), (float(v) for v in base), strict=True))
        self._send(encode_message(TOPIC_STATE, header))
        jpegs = [self._encode(images.get(camera)) for camera in CAMERAS]
        header[CAMERAS_KEY] = list(CAMERAS)
        header[SIZES_KEY] = [len(jpeg) for jpeg in jpegs]
        self._send(encode_message(TOPIC_OBS, header, b"".join(jpegs)))
        return True

    def _send(self, message: bytes) -> None:
        if self._pub is None:
            return
        try:
            self._pub.send(message, zmq.NOBLOCK)
        except zmq.Again:
            return  # a slow subscriber drops this tick rather than stalling the robot

    def _encode(self, image_bgr: np.ndarray | None) -> bytes:
        if image_bgr is None:
            return b""
        ok, jpeg = cv2.imencode(".jpg", image_bgr, self._jpeg_params)
        return jpeg.tobytes() if ok else b""

    def _apply(self, payload: dict, now: float) -> None:
        joints = _floats(payload, JOINT_KEYS, required=True)
        base = _floats(payload, BASE_KEYS, required=False)
        if joints is None or base is None:
            self._log(f"LeRobot bridge ignored a malformed action: {payload}")
            return
        self._last_action = now
        self._base_stopped = False
        if self.commands_blocked:
            return
        self._on_arm(joints)
        self._on_base(base[0], base[1])

    def _watchdog(self, now: float) -> None:
        if self._last_action is None or self._base_stopped:
            return
        if now - self._last_action > self._watchdog_s:
            self._base_stopped = True
            self._on_stop_base()


def encode_message(topic: bytes, header: dict, payload: bytes = b"") -> bytes:
    return topic + b" " + json.dumps(header).encode() + b"\n" + payload


def _parse(raw: bytes) -> dict | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _floats(payload: dict, keys: Sequence[str], *, required: bool) -> list[float] | None:
    values: list[float] = []
    for key in keys:
        if key not in payload:
            if required:
                return None
            values.append(0.0)
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        values.append(value)
    return values
