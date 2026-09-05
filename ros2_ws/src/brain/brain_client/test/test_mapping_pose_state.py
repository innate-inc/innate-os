# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Tests for map-pose authority selection across navigation lifecycle modes."""

import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ros_skill_test_stubs import install_if_ros_is_missing  # noqa: E402

install_if_ros_is_missing()

from rclpy.qos import QoSDurabilityPolicy, QoSReliabilityPolicy  # noqa: E402

from brain_client.skills.robot_state import NAV_MODE_QOS, RobotStateProvider  # noqa: E402
from brain_client.skills.types import Skill  # noqa: E402
from brain_client.state.nav_mode import NavMode  # noqa: E402
from brain_client.state.pose import Pose  # noqa: E402


class _PoseConsumer(Skill):
    nav_mode: NavMode
    pose: Pose | None = None

    def execute(self):
        pass


class _Logger:
    def __init__(self):
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, _message: str) -> None:
        pass


class _FeedNode:
    def __init__(self):
        self.subscriptions: list[SimpleNamespace] = []

    def create_subscription(self, message_type, topic, callback, qos):
        subscription = SimpleNamespace(message_type=message_type, topic=topic, callback=callback, qos=qos)
        self.subscriptions.append(subscription)
        return subscription


class _Manipulation:
    def __init__(self):
        self.node = _FeedNode()
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


def _provider() -> RobotStateProvider:
    logger = _Logger()
    owner = SimpleNamespace(get_logger=lambda: logger)
    manipulation = _Manipulation()
    provider = RobotStateProvider(
        owner,
        None,
        manipulation=manipulation,
        mobility=None,
        head=None,
        memory=None,
        head_current_position_topic="/head/current_position",
    )
    provider._active = True
    return provider


def _mode(value: str) -> SimpleNamespace:
    return SimpleNamespace(data=value)


def _pose_message(x: float, y: float, yaw: float, *, frame_id: str = "map", stamp: float = 12.25):
    sec = math.floor(stamp)
    nanosec = round((stamp - sec) * 1e9)
    pose = SimpleNamespace(
        position=SimpleNamespace(x=x, y=y),
        orientation=SimpleNamespace(x=0.0, y=0.0, z=math.sin(yaw / 2.0), w=math.cos(yaw / 2.0)),
    )
    return SimpleNamespace(
        header=SimpleNamespace(frame_id=frame_id, stamp=SimpleNamespace(sec=sec, nanosec=nanosec)),
        pose=SimpleNamespace(pose=pose),
    )


def test_current_pose_selects_amcl_in_navigation_and_mapping_pose_in_mapping():
    provider = _provider()
    amcl = _pose_message(1.0, 2.0, 0.25, stamp=3.5)
    mapping = _pose_message(8.0, 9.0, -0.5, stamp=4.75)

    provider._on_nav_mode(_mode(" Navigation "))
    provider._on_amcl_pose(amcl)
    provider._on_mapping_pose(mapping)
    pose = provider.current_pose()
    assert (pose.x, pose.y, pose.theta, pose.stamp) == pytest.approx((1.0, 2.0, 0.25, 3.5))
    assert pose.frame_id == "map"

    # Even if the inactive source received a message, changing authorities
    # must wait for a new sample from the newly active source.
    provider._on_nav_mode(_mode("autonomous_mapping"))
    assert provider.current_pose() is None
    assert provider.last_amcl_pose is None
    assert provider.last_mapping_pose is None

    provider._on_mapping_pose(mapping)
    pose = provider.current_pose()
    assert (pose.x, pose.y, pose.theta, pose.stamp) == pytest.approx((8.0, 9.0, -0.5, 4.75))
    assert provider.current_nav_mode().value == "autonomous_mapping"
    assert provider.current_nav_mode().is_mapping
    assert provider.current_nav_mode().supports_map_navigation


def test_pose_conversion_is_cached_per_sensor_sample():
    provider = _provider()
    provider._on_nav_mode(_mode("autonomous_mapping"))
    first_message = _pose_message(1.0, 2.0, 0.25, stamp=3.5)
    provider._on_mapping_pose(first_message)

    first = provider.current_pose()
    assert provider.current_pose() is first

    provider._on_mapping_pose(_pose_message(1.1, 2.1, 0.3, stamp=3.6))
    second = provider.current_pose()
    assert second is not first
    assert second.position == pytest.approx((1.1, 2.1))


def test_mode_change_clears_already_injected_pose_until_new_authority_sample():
    provider = _provider()
    skill = _PoseConsumer(logging.getLogger("test_pose_consumer"))
    provider._on_nav_mode(_mode("navigation"))
    provider._on_amcl_pose(_pose_message(1.0, 2.0, 0.25, stamp=3.5))
    provider.update_skill_robot_state(skill)
    assert skill.pose.position == pytest.approx((1.0, 2.0))
    assert skill.nav_mode.value == "navigation"

    provider._on_nav_mode(_mode("autonomous_mapping"))
    provider.update_skill_robot_state(skill)
    assert skill.pose is None
    assert skill.nav_mode.value == "autonomous_mapping"

    provider._on_mapping_pose(_pose_message(8.0, 9.0, -0.5, stamp=4.75))
    provider.update_skill_robot_state(skill)
    assert skill.pose.position == pytest.approx((8.0, 9.0))


def test_authority_transition_does_not_reuse_cached_inactive_pose_or_odom():
    provider = _provider()
    mapping = _pose_message(4.0, 5.0, 0.75)
    cached_amcl = _pose_message(-10.0, -11.0, -0.25)

    provider._on_nav_mode(_mode("mapping"))
    provider._on_mapping_pose(mapping)
    provider._on_amcl_pose(cached_amcl)
    assert provider.current_pose().position == pytest.approx((4.0, 5.0))

    provider._on_nav_mode(_mode("navigation"))
    assert provider.current_pose() is None
    provider._on_amcl_pose(_pose_message(6.0, 7.0, 1.0))
    assert provider.current_pose().position == pytest.approx((6.0, 7.0))

    provider.last_odom = _pose_message(99.0, 100.0, 0.0, frame_id="odom")
    provider._on_nav_mode(_mode("mapfree"))
    assert provider.current_pose() is None


def test_current_pose_rejects_non_map_frame_from_active_authority():
    provider = _provider()
    provider._on_nav_mode(_mode("mapping"))
    provider._on_mapping_pose(_pose_message(1.0, 2.0, 0.0, frame_id="odom"))

    assert provider.current_pose() is None
    assert provider._logger.warnings == [
        "Skill requires map-frame pose but none available (yet); will inject when it arrives."
    ]


def test_nav_mode_subscription_requests_latched_reliable_status():
    provider = _provider()
    provider._active = False

    provider.start_subscriptions()

    subscription = next(sub for sub in provider._manipulation.node.subscriptions if sub.topic == "/nav/current_mode")
    assert subscription.qos is NAV_MODE_QOS
    assert subscription.qos.depth == 1
    assert subscription.qos.reliability == QoSReliabilityPolicy.RELIABLE
    assert subscription.qos.durability == QoSDurabilityPolicy.TRANSIENT_LOCAL
