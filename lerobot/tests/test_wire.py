"""The client and the on-robot bridge speak the same protocol."""

from __future__ import annotations

import threading
import time
from types import ModuleType

import numpy as np
import pytest
from lerobot.utils.errors import DeviceNotConnectedError

from lerobot_robot_mars import Mars, MarsConfig, MarsPassthroughTeleop, MarsPassthroughTeleopConfig, wire
from lerobot_robot_mars.schema import ACTION_NAMES, CAMERA_ORDER, CAMERA_SHAPE, STATE_NAMES

from .conftest import free_port

JOINTS = [0.1, -0.2, 0.3, -0.4, 0.5, 0.6]
COMMANDED = [0.11, -0.21, 0.31, -0.41, 0.51, 0.61]
BASE = (0.25, -0.5)


def bgr(b: int, g: int, r: int) -> np.ndarray:
    frame = np.zeros(CAMERA_SHAPE, dtype=np.uint8)
    frame[:] = (b, g, r)
    return frame


class FakeRobot:
    """Runs the real bridge the way manipulation_server does, at 30 Hz, on a thread."""

    def __init__(self, bridge_module: ModuleType, *, watchdog_s: float = 0.3, idle_after_s: float = 1.0) -> None:
        self.arm: list[list[float]] = []
        self.base: list[tuple[float, float]] = []
        self.stops = 0
        self.port_actions = free_port()
        self.port_observations = free_port()
        self.bridge = bridge_module.LeRobotBridge(
            self.arm.append,
            lambda vx, wz: self.base.append((vx, wz)),
            self._stop,
            port_actions=self.port_actions,
            port_observations=self.port_observations,
            bind_address="127.0.0.1",
            watchdog_s=watchdog_s,
            idle_after_s=idle_after_s,
        )
        self.images = {"head": bgr(255, 0, 0), "wrist": bgr(0, 255, 0)}
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _stop(self) -> None:
        self.stops += 1

    def __enter__(self) -> FakeRobot:
        self.bridge.bind()
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2)
        self.bridge.close()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            now = time.monotonic()
            self.bridge.poll(now)
            self.bridge.publish(now, joints=JOINTS, commanded=COMMANDED, base=BASE, images=self.images, busy=False)
            time.sleep(1 / 30)


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_protocol_constants_match(bridge_module: ModuleType) -> None:
    assert bridge_module.TOPIC_STATE == wire.TOPIC_STATE
    assert bridge_module.TOPIC_OBS == wire.TOPIC_OBS
    assert bridge_module.HEARTBEAT_KEY == wire.HEARTBEAT_KEY
    assert bridge_module.CAMERAS_KEY == wire.CAMERAS_KEY
    assert bridge_module.SIZES_KEY == wire.SIZES_KEY
    assert bridge_module.COMMAND_PREFIX == wire.COMMAND_PREFIX
    assert bridge_module.DEFAULT_PORT_ACTIONS == wire.DEFAULT_PORT_ACTIONS
    assert bridge_module.DEFAULT_PORT_OBSERVATIONS == wire.DEFAULT_PORT_OBSERVATIONS
    assert bridge_module.JOINT_KEYS == STATE_NAMES
    assert bridge_module.JOINT_KEYS + bridge_module.BASE_KEYS == ACTION_NAMES
    assert bridge_module.CAMERAS == CAMERA_ORDER


def test_observation_round_trip(bridge_module: ModuleType) -> None:
    with FakeRobot(bridge_module) as robot:
        mars = Mars(
            MarsConfig(
                remote_ip="127.0.0.1", port_actions=robot.port_actions, port_observations=robot.port_observations
            )
        )
        mars.connect()
        try:
            observation = mars.get_observation()
        finally:
            mars.disconnect()

    assert set(observation) == set(mars.observation_features)
    assert [observation[name] for name in STATE_NAMES] == pytest.approx(JOINTS)
    head, wrist = observation["head"], observation["wrist"]
    assert head.shape == CAMERA_SHAPE and head.dtype == np.uint8
    # The bridge encoded pure blue in BGR; the client must hand back RGB.
    assert head.mean(axis=(0, 1)).argmax() == 2
    assert wrist.mean(axis=(0, 1)).argmax() == 1


def test_actions_reach_the_robot_and_the_watchdog_fires_once(bridge_module: ModuleType) -> None:
    with FakeRobot(bridge_module, watchdog_s=0.3) as robot:
        mars = Mars(
            MarsConfig(
                remote_ip="127.0.0.1", port_actions=robot.port_actions, port_observations=robot.port_observations
            )
        )
        mars.connect()
        try:
            action = dict(zip(STATE_NAMES, JOINTS, strict=True)) | {"x.vel": 0.2, "theta.vel": -0.1}
            sent = mars.send_action(action)
            assert list(sent) == list(ACTION_NAMES)
            assert wait_until(lambda: robot.arm and robot.base)
            assert robot.arm[-1] == pytest.approx(JOINTS)
            assert robot.base[-1] == pytest.approx((0.2, -0.1))
            assert wait_until(lambda: robot.stops == 1)
            time.sleep(0.5)
            assert robot.stops == 1
            with pytest.raises(ValueError):
                mars.send_action({"x.vel": 0.1})
        finally:
            mars.disconnect()


def test_external_commands_only_heartbeat(bridge_module: ModuleType) -> None:
    with FakeRobot(bridge_module) as robot:
        mars = Mars(
            MarsConfig(
                remote_ip="127.0.0.1",
                port_actions=robot.port_actions,
                port_observations=robot.port_observations,
                external_commands=True,
            )
        )
        mars.connect()
        try:
            for _ in range(5):
                mars.send_action(dict(zip(STATE_NAMES, JOINTS, strict=True)))
                mars.get_observation()
                time.sleep(0.05)
            assert robot.arm == [] and robot.base == [] and robot.stops == 0
            assert robot.bridge.active(time.monotonic())
        finally:
            mars.disconnect()


def test_blocked_commands_are_ignored(bridge_module: ModuleType) -> None:
    with FakeRobot(bridge_module) as robot:
        robot.bridge.commands_blocked = True
        mars = Mars(
            MarsConfig(
                remote_ip="127.0.0.1", port_actions=robot.port_actions, port_observations=robot.port_observations
            )
        )
        mars.connect()
        try:
            mars.send_action(dict(zip(STATE_NAMES, JOINTS, strict=True)))
            time.sleep(0.2)
            assert robot.arm == []
        finally:
            mars.disconnect()


def test_passthrough_teleop_reads_the_commanded_target(bridge_module: ModuleType) -> None:
    with FakeRobot(bridge_module) as robot:
        teleop = MarsPassthroughTeleop(
            MarsPassthroughTeleopConfig(
                remote_ip="127.0.0.1", port_actions=robot.port_actions, port_observations=robot.port_observations
            )
        )
        teleop.connect()
        try:
            action = teleop.get_action()
        finally:
            teleop.disconnect()
    assert list(action) == list(ACTION_NAMES)
    assert [action[name] for name in STATE_NAMES] == pytest.approx(COMMANDED)
    assert (action["x.vel"], action["theta.vel"]) == pytest.approx(BASE)


def test_bridge_idles_without_a_client(bridge_module: ModuleType) -> None:
    with FakeRobot(bridge_module, idle_after_s=0.3) as robot:
        assert not robot.bridge.active(time.monotonic())
        mars = Mars(
            MarsConfig(
                remote_ip="127.0.0.1", port_actions=robot.port_actions, port_observations=robot.port_observations
            )
        )
        mars.connect()
        assert robot.bridge.active(time.monotonic())
        mars.disconnect()
        assert wait_until(lambda: not robot.bridge.active(time.monotonic()))


def test_connect_fails_fast_without_a_bridge() -> None:
    mars = Mars(
        MarsConfig(
            remote_ip="127.0.0.1", port_actions=free_port(), port_observations=free_port(), connect_timeout_s=0.5
        )
    )
    with pytest.raises(DeviceNotConnectedError):
        mars.connect()
    assert not mars.is_connected
