# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS-free tests for the LeRobot bridge's protocol and safety behaviour.

Run with ``python -m pytest test/test_lerobot_bridge.py -q`` from the package root; the plugin
half of the protocol is tested in innate-os/lerobot/tests against this same module.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import numpy as np
import pytest

zmq = pytest.importorskip("zmq")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manipulation.lerobot_bridge import (  # noqa: E402
    CAMERAS,
    CAMERAS_KEY,
    JOINT_KEYS,
    SIZES_KEY,
    TOPIC_OBS,
    TOPIC_STATE,
    LeRobotBridge,
)

JOINTS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, **kwargs: float) -> None:
        self.arm: list[list[float]] = []
        self.base: list[tuple[float, float]] = []
        self.heads: list[float] = []
        self.stops = 0
        self.port_actions = free_port()
        self.port_observations = free_port()
        self.bridge = LeRobotBridge(
            self.arm.append,
            lambda vx, wz: self.base.append((vx, wz)),
            self._stop,
            port_actions=self.port_actions,
            port_observations=self.port_observations,
            bind_address="127.0.0.1",
            on_head=self.heads.append,
            **kwargs,
        )
        self.context = zmq.Context()
        self.push = self.context.socket(zmq.PUSH)
        self.sub = self.context.socket(zmq.SUB)

    def _stop(self) -> None:
        self.stops += 1

    def __enter__(self) -> Harness:
        self.bridge.bind()
        self.push.setsockopt(zmq.LINGER, 0)
        self.push.connect(f"tcp://127.0.0.1:{self.port_actions}")
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")
        self.sub.connect(f"tcp://127.0.0.1:{self.port_observations}")
        time.sleep(0.1)  # let the subscription propagate before the first publish
        return self

    def __exit__(self, *exc: object) -> None:
        self.push.close()
        self.sub.close()
        self.context.term()
        self.bridge.close()

    def send(self, payload: dict) -> None:
        self.push.send_string(json.dumps(payload))
        time.sleep(0.05)

    def receive(self) -> dict[bytes, tuple[dict, bytes]]:
        messages: dict[bytes, tuple[dict, bytes]] = {}
        while self.sub.poll(200):
            raw = self.sub.recv()
            topic, _, rest = raw.partition(b" ")
            head, _, payload = rest.partition(b"\n")
            messages[topic] = (json.loads(head), payload)
        return messages


def test_action_is_applied_and_the_watchdog_stops_the_base_once() -> None:
    with Harness(watchdog_s=0.5) as h:
        h.send(dict(zip(JOINT_KEYS, JOINTS, strict=True)) | {"x.vel": 0.3})
        h.bridge.poll(now=1.0)
        assert h.arm == [pytest.approx(JOINTS)]
        assert h.base == [(0.3, 0.0)]
        h.bridge.poll(now=1.4)
        assert h.stops == 0
        h.bridge.poll(now=1.6)
        h.bridge.poll(now=5.0)
        assert h.stops == 1


def test_malformed_actions_are_ignored() -> None:
    with Harness() as h:
        h.send({"joint1.pos": 0.1})
        h.send(dict(zip(JOINT_KEYS, [float("nan")] * 6, strict=True)))
        h.push.send(b"not json")
        time.sleep(0.05)
        h.bridge.poll(now=1.0)
        assert h.arm == [] and h.base == []
        assert h.bridge.active(1.0)  # still counts as a live client


def test_blocked_bridge_keeps_hands_off() -> None:
    with Harness() as h:
        h.bridge.commands_blocked = True
        h.send(dict(zip(JOINT_KEYS, JOINTS, strict=True)))
        h.bridge.poll(now=1.0)
        assert h.arm == [] and h.base == []
        h.bridge.poll(now=5.0)
        assert h.stops == 0  # a stop would land on the channel the running behavior drives through


def test_a_behavior_starting_mid_drive_gets_one_stop_up_front() -> None:
    with Harness(watchdog_s=0.5) as h:
        h.send(dict(zip(JOINT_KEYS, JOINTS, strict=True)) | {"x.vel": 0.3})
        h.bridge.poll(now=1.0)
        h.bridge.commands_blocked = True
        h.bridge.poll(now=1.1)
        h.bridge.poll(now=5.0)
        assert h.stops == 1


def test_publish_carries_state_and_jpegs_only_while_active() -> None:
    with Harness() as h:
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        published = h.bridge.publish(
            0.0, joints=JOINTS, commanded=JOINTS, base=(0.0, 0.0), images={"head": image, "wrist": None}, busy=False
        )
        assert not published
        h.send({"_hb": 1})
        h.bridge.poll(now=1.0)
        assert h.bridge.publish(
            1.0,
            joints=JOINTS,
            commanded=[0.0] * 6,
            base=(0.1, 0.2),
            images={"head": image, "wrist": None},
            busy=True,
            head_deg=-19.5,
        )
        messages = h.receive()
        state, _ = messages[TOPIC_STATE]
        assert [state[k] for k in JOINT_KEYS] == pytest.approx(JOINTS)
        assert [state["cmd." + k] for k in JOINT_KEYS] == [0.0] * 6
        assert (state["cmd.x.vel"], state["cmd.theta.vel"]) == (0.1, 0.2)
        assert state["busy"] is True and state["seq"] == 1
        assert state["head.deg"] == -19.5
        header, payload = messages[TOPIC_OBS]
        assert header[CAMERAS_KEY] == list(CAMERAS)
        assert header[SIZES_KEY] == [len(payload), 0]
        assert payload[:2] == b"\xff\xd8"
