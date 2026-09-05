"""Servo-load scaling and ROS publication; run with sim and ROS runtimes."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ros2_ws/src/mars_bot/mars_sim_driver"))


def test_simulated_effort_uses_each_servos_torque_limit():
    pytest.importorskip("mujoco")
    from mars_sim_driver.core import VirtualMars

    sim = SimpleNamespace(
        _joints={"joint1": (0, 0, 0), "joint6": (1, 1, 0)},
        _servo={0: (5, 2.0), 1: (1, 0.5)},
        data=SimpleNamespace(qfrc_applied=[-1.0, 0.5]),
    )
    assert VirtualMars.joint_efforts(sim) == {"joint1": -50.0, "joint6": 100.0}


@pytest.mark.parametrize("available", [True, False])
def test_remote_effort_reaches_both_ros_publishers(available):
    pytest.importorskip("rclpy")
    from builtin_interfaces.msg import Time
    from mars_sim_driver.node import ARM_JOINTS, VirtualMarsNode
    from mars_sim_driver.remote_world import RemoteWorld

    names = [*ARM_JOINTS, "joint_head"]
    efforts = {n: float(i - 3) for i, n in enumerate(reversed(names))}
    snapshot = {"joints": dict.fromkeys(names, 0.1)}
    if available:
        snapshot["efforts"] = efforts
    remote = object.__new__(RemoteWorld)
    remote._fresh_state = lambda: snapshot
    messages = []
    publisher = SimpleNamespace(publish=messages.append)
    node = SimpleNamespace(
        _lock=threading.Lock(),
        sim=remote,
        _stamp=Time,
        _arm_state_pub=publisher,
        _joint_states_pub=publisher,
    )
    VirtualMarsNode._publish_arm_state(node)
    VirtualMarsNode._publish_joint_states(node)
    for message, expected_names in zip(messages, [ARM_JOINTS, names], strict=True):
        assert list(message.name) == expected_names
        assert list(message.effort) == ([efforts[n] for n in expected_names] if available else [])
