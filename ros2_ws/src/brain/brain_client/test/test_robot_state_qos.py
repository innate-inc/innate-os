# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from types import SimpleNamespace

from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import OccupancyGrid
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.skills.robot_state import RobotStateProvider


class _FeedNode:
    def __init__(self):
        self.subscriptions = {}

    def create_subscription(self, _message_type, topic, callback, qos):
        self.subscriptions[topic] = (callback, qos)
        return object()


def test_map_and_pose_subscriptions_request_latched_state():
    state_node = _FeedNode()
    state_node.get_logger = lambda: SimpleNamespace()
    high_rate_node = _FeedNode()
    manipulation = SimpleNamespace(node=high_rate_node, start=lambda: None, stop=lambda: None)
    provider = RobotStateProvider(
        state_node,
        None,
        manipulation=manipulation,
        mobility=None,
        head=None,
        memory=None,
        head_current_position_topic="/head/current_position",
    )

    provider.start_subscriptions()

    for topic in ("/map", "/amcl_pose"):
        _callback, qos = state_node.subscriptions[topic]
        assert isinstance(qos, QoSProfile)
        assert qos.reliability == QoSReliabilityPolicy.RELIABLE
        assert qos.history == QoSHistoryPolicy.KEEP_LAST
        assert qos.depth == 1
        assert qos.durability == QoSDurabilityPolicy.TRANSIENT_LOCAL
        assert topic not in high_rate_node.subscriptions


def test_latched_map_and_pose_remain_current_between_skill_runs():
    state_node = _FeedNode()
    state_node.get_logger = lambda: SimpleNamespace()
    high_rate_node = _FeedNode()
    manipulation = SimpleNamespace(node=high_rate_node, start=lambda: None, stop=lambda: None)
    provider = RobotStateProvider(
        state_node,
        None,
        manipulation=manipulation,
        mobility=None,
        head=None,
        memory=None,
        head_current_position_topic="/head/current_position",
    )
    provider.start_subscriptions()

    map_callback, _map_qos = state_node.subscriptions["/map"]
    pose_callback, _pose_qos = state_node.subscriptions["/amcl_pose"]
    first_map = OccupancyGrid()
    first_pose = PoseWithCovarianceStamped()
    map_callback(first_map)
    pose_callback(first_pose)

    provider.stop_subscriptions()
    assert provider.last_map is first_map
    assert provider.last_amcl_pose is first_pose

    # These callbacks live on the always-spinning node, so a map/pose update
    # while idle replaces the cached state before the next skill starts.
    latest_map = OccupancyGrid()
    latest_pose = PoseWithCovarianceStamped()
    map_callback(latest_map)
    pose_callback(latest_pose)
    provider.start_subscriptions()

    assert provider.last_map is latest_map
    assert provider.last_amcl_pose is latest_pose
