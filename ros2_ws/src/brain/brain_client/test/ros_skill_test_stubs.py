# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Minimal ROS module shims for host-side workspace-skill unit tests.

The integration image has the real packages. macOS host tests do not, and the
orchestration under test is duck-typed, so install only the import surface the
skill modules need when ROS is absent.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace


def install_if_ros_is_missing() -> None:
    try:
        from action_msgs.msg import GoalStatus  # noqa: F401
        from brain_messages.srv import ChangeNavigationMode  # noqa: F401
        from geometry_msgs.msg import PoseStamped  # noqa: F401
        from nav2_msgs.action import NavigateToPose  # noqa: F401
        from rclpy.action import ActionClient  # noqa: F401
        from rclpy.node import Node  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    # Preserve the real local brain_client package path before registering
    # placeholder child modules below.
    __import__("brain_client.robot")

    def module(name: str, *, package: bool = False) -> ModuleType:
        existing = sys.modules.get(name)
        if existing is not None:
            return existing
        created = ModuleType(name)
        if package:
            created.__path__ = []
        sys.modules[name] = created
        if "." in name:
            parent_name, _, child_name = name.rpartition(".")
            parent = module(parent_name, package=True)
            setattr(parent, child_name, created)
        return created

    rclpy_node = module("rclpy.node")
    rclpy_action = module("rclpy.action")
    rclpy_qos = module("rclpy.qos")
    action_msgs_msg = module("action_msgs.msg")
    std_msgs_msg = module("std_msgs.msg")
    geometry_msgs_msg = module("geometry_msgs.msg")
    nav_msgs_msg = module("nav_msgs.msg")
    sensor_msgs_msg = module("sensor_msgs.msg")
    brain_messages_srv = module("brain_messages.srv")
    nav2_msgs_action = module("nav2_msgs.action")
    navigator_module = module("nav2_simple_commander.robot_navigator")
    # Skill annotation materialization imports every robot interface to build
    # its type table. The tests exercise only state feeds, so placeholders are
    # sufficient and avoid pulling their larger ROS message surfaces.
    robot_head = module("brain_client.robot.head")
    robot_manipulation = module("brain_client.robot.manipulation")
    robot_mobility = module("brain_client.robot.mobility")
    robot_memory = module("brain_client.robot.spatial_memory")

    class QoSProfile:
        def __init__(self, *, depth, durability=None, history=None, reliability=None):
            self.depth = depth
            self.durability = durability
            self.history = history
            self.reliability = reliability

    class DurabilityPolicy:
        TRANSIENT_LOCAL = "transient_local"

    class ReliabilityPolicy:
        BEST_EFFORT = "best_effort"
        RELIABLE = "reliable"

    class HistoryPolicy:
        KEEP_LAST = "keep_last"

    class PoseStamped:
        def __init__(self):
            self.header = SimpleNamespace(frame_id="", stamp=None)
            self.pose = SimpleNamespace(
                position=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
            )

    class Twist:
        def __init__(self):
            self.linear = SimpleNamespace(x=0.0, y=0.0, z=0.0)
            self.angular = SimpleNamespace(x=0.0, y=0.0, z=0.0)

    class String:
        def __init__(self, *, data=""):
            self.data = data

    class ChangeNavigationMode:
        class Request:
            def __init__(self):
                self.mode = ""

    class NavigateToPose:
        class Goal:
            def __init__(self):
                self.pose = None
                self.behavior_tree = ""

    class GoalStatus:
        STATUS_UNKNOWN = 0
        STATUS_ACCEPTED = 1
        STATUS_EXECUTING = 2
        STATUS_CANCELING = 3
        STATUS_SUCCEEDED = 4
        STATUS_CANCELED = 5
        STATUS_ABORTED = 6

    class TaskResult:
        SUCCEEDED = "succeeded"

    class BatteryState:
        POWER_SUPPLY_STATUS_CHARGING = 1

    rclpy_node.Node = object
    rclpy_action.ActionClient = type("ActionClient", (), {})
    rclpy_qos.DurabilityPolicy = DurabilityPolicy
    rclpy_qos.QoSDurabilityPolicy = DurabilityPolicy
    rclpy_qos.QoSHistoryPolicy = HistoryPolicy
    rclpy_qos.QoSReliabilityPolicy = ReliabilityPolicy
    rclpy_qos.QoSProfile = QoSProfile
    rclpy_qos.qos_profile_sensor_data = object()
    action_msgs_msg.GoalStatus = GoalStatus
    std_msgs_msg.String = String
    geometry_msgs_msg.PoseStamped = PoseStamped
    geometry_msgs_msg.PoseWithCovarianceStamped = type("PoseWithCovarianceStamped", (), {})
    geometry_msgs_msg.Twist = Twist
    nav_msgs_msg.OccupancyGrid = type("OccupancyGrid", (), {})
    nav_msgs_msg.Odometry = type("Odometry", (), {})
    sensor_msgs_msg.BatteryState = BatteryState
    sensor_msgs_msg.CompressedImage = type("CompressedImage", (), {})
    sensor_msgs_msg.JointState = type("JointState", (), {})
    sensor_msgs_msg.LaserScan = type("LaserScan", (), {})
    brain_messages_srv.ChangeNavigationMode = ChangeNavigationMode
    nav2_msgs_action.NavigateToPose = NavigateToPose
    navigator_module.BasicNavigator = type("BasicNavigator", (), {})
    navigator_module.TaskResult = TaskResult
    robot_head.Head = type("Head", (), {})
    robot_manipulation.Manipulation = type("Manipulation", (), {})
    robot_mobility.Mobility = type("Mobility", (), {})
    robot_memory.SpatialMemory = type("SpatialMemory", (), {})
