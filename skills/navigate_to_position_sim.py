import math
import threading
import time
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path
from std_msgs.msg import String, Bool
from nav2_simple_commander.robot_navigator import BasicNavigator
from brain_client.skill_types import Skill, SkillResult
import rclpy
from rclpy.node import Node


class SimPathPlanningController:
    def __init__(self, logger, primitive):
        """
        Initialize the SimPathPlanningController using Nav2 for path planning
        and ROS2 topics for sending the plan to the simulator
        """
        self.logger = logger
        self._send_feedback = primitive._send_feedback

        # Create Nav2 navigators with namespaces (matching real navigate_to_position.py)
        self.navigator = BasicNavigator(namespace="")
        self.navigator_mapfree = BasicNavigator(namespace="mapfree")
        self.navigator_navigation = BasicNavigator(namespace="navigation")

        # Create a minimal ROS2 node for publishing
        if not rclpy.ok():
            rclpy.init()
        self.node = Node("path_planning_controller")

        # Topic-based communication for sending path to simulator
        self.path_publisher = self.node.create_publisher(
            Path, "/sim_navigation/global_plan", 10
        )
        self.cancel_publisher = self.node.create_publisher(
            Bool, "/sim_navigation/cancel", 10
        )

        # Subscribe to execution status from simulator
        self.status_subscriber = self.node.create_subscription(
            String, "/sim_navigation/status", self._status_callback, 10
        )

        # Navigation state
        self._navigation_status = "IDLE"  # IDLE, ACTIVE, SUCCEEDED, FAILED, CANCELED
        self._cancel_requested = threading.Event()
        self._navigation_complete = threading.Event()

        self.logger.info("Simulator path planning controller created")

    def _status_callback(self, msg):
        """Callback for navigation status updates from simulator"""
        self._navigation_status = msg.data
        if self._navigation_status in ["SUCCEEDED", "FAILED", "CANCELED"]:
            self._navigation_complete.set()
        self.logger.info(f"Navigation status: {self._navigation_status}")

    def go_to_position(
        self, x: float, y: float, theta: float, local_frame: bool = False
    ):
        """
        Plans a path to the goal and sends it to the simulator for execution.

        Args:
            x (float): x-coordinate of the target position.
            y (float): y-coordinate of the target position.
            theta (float): The orientation angle in radians.
            local_frame (bool): If True, coordinates are relative to robot's current position (base_link).
                               If False, coordinates are in the map frame.

        Returns:
            str: Navigation result ("SUCCEEDED", "FAILED", "CANCELED")
        """
        # Reset state
        self._cancel_requested.clear()
        self._navigation_complete.clear()
        self._navigation_status = "IDLE"

        # Create goal pose in original frame
        goal_pose = PoseStamped()
        goal_pose.header.frame_id = "base_link" if local_frame else "map"
        goal_pose.header.stamp = self.navigator.get_clock().now().to_msg()
        goal_pose.pose.position.x = x
        goal_pose.pose.position.y = y
        goal_pose.pose.position.z = 0.0

        # Convert theta to quaternion
        goal_pose.pose.orientation.x = 0.0
        goal_pose.pose.orientation.y = 0.0
        goal_pose.pose.orientation.z = math.sin(theta / 2.0)
        goal_pose.pose.orientation.w = math.cos(theta / 2.0)

        self.logger.info(
            f"[NavSim] Planning path to: x={x}, y={y}, theta={theta}, frame={goal_pose.header.frame_id}, local_frame={local_frame}"
        )

        # Use the correct navigator based on local_frame (matching real navigate_to_position.py)
        path_navigator = (
            self.navigator_mapfree if local_frame else self.navigator_navigation
        )

        # Get the global path from Nav2
        self.logger.info(
            f"[NavSim] Requesting path from Nav2 (namespace={'mapfree' if local_frame else 'navigation'}) "
            f"with goal_pose: pos=({goal_pose.pose.position.x:.2f}, {goal_pose.pose.position.y:.2f}), frame={goal_pose.header.frame_id}"
        )
        path = path_navigator.getPath(goal_pose, goal_pose, use_start=False)

        if path is None:
            self.logger.error("[NavSim] Failed to get path to goal")
            return "FAILED"

        self.logger.info(f"[NavSim] Generated path with {len(path.poses)} waypoints")
        if path.poses:
            first_pose = path.poses[0].pose.position
            last_pose = path.poses[-1].pose.position
            self.logger.info(
                f"[NavSim] Path: first=({first_pose.x:.2f}, {first_pose.y:.2f}), last=({last_pose.x:.2f}, {last_pose.y:.2f}), frame={path.header.frame_id}"
            )

        # Send the path and goal to the simulator
        self.path_publisher.publish(path)

        self.logger.info(
            "[NavSim] Path sent to simulator via /sim_navigation/global_plan"
        )

        # Wait for simulator to complete navigation or handle cancellation
        while not self._navigation_complete.is_set():
            if self._cancel_requested.is_set():
                self.logger.info("Cancellation detected, sending cancel message")
                cancel_msg = Bool()
                cancel_msg.data = True
                self.cancel_publisher.publish(cancel_msg)
                self._navigation_status = "CANCELED"
                break

            # Spin the node to process callbacks
            rclpy.spin_once(self.node, timeout_sec=0.1)
            time.sleep(0.1)

        return self._navigation_status

    def cancel_navigation(self):
        """Cancels the current navigation task."""
        self.logger.debug("Canceling current navigation task...")
        self._cancel_requested.set()


class NavigateToPositionSim(Skill):
    def __init__(self, logger):
        self.path_controller = SimPathPlanningController(logger, self)
        self.logger = logger

    @property
    def name(self):
        return "navigate_to_position_sim"

    def guidelines(self):
        return (
            "Use when you need to navigate the robot to the specified position "
            "using provided x, y coordinates, and theta (yaw) angle IN RADIANS. "
            "If local_frame is set to false, it navigates to a specific point in the map. "
            "If local_frame is set to true, it navigates locally, where the robot is currently (0,0). "
            "This version uses Nav2 for path planning but sends the plan to the simulator for execution."
        )

    def execute(self, x: float, y: float, theta: float, local_frame: bool = False):
        self.logger.info(
            f"[NavSim] execute() called with: x={x}, y={y}, theta={theta} ({math.degrees(theta):.1f}°), local_frame={local_frame}"
        )

        result = self.path_controller.go_to_position(x, y, theta, local_frame)

        # Process result
        if result == "CANCELED":
            self.logger.info("Navigation was canceled")
            return "Navigation canceled", SkillResult.CANCELLED
        elif result == "SUCCEEDED":
            self.logger.info(
                f"Navigation complete. Arrived at position: x={x}, y={y}, theta={theta}, local_frame={local_frame}"
            )
            return f"Reached position ({x}, {y}, {theta})", SkillResult.SUCCESS
        else:
            self.logger.info(f"Navigation failed with result: {result}")
            return f"Navigation failed with result: {result}", SkillResult.FAILURE

    def cancel(self):
        """Cancels the current navigation task."""
        self.logger.debug("Canceling navigation task")
        self.path_controller.cancel_navigation()
        return "Navigation canceled"
