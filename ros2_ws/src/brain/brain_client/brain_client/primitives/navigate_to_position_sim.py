import math
import threading
import time
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path
from std_msgs.msg import String, Bool
from nav2_simple_commander.robot_navigator import BasicNavigator
from brain_client.primitives.types import Primitive, PrimitiveResult
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

        # Create Nav2 navigator for path planning only
        self.navigator = BasicNavigator()

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

    def go_to_position(self, x: float, y: float, theta: float):
        """
        Plans a path to the goal and sends it to the simulator for execution.

        Args:
            x (float): x-coordinate of the target position.
            y (float): y-coordinate of the target position.
            theta (float): The orientation angle in radians.

        Returns:
            str: Navigation result ("SUCCEEDED", "FAILED", "CANCELED")
        """
        # Reset state
        self._cancel_requested.clear()
        self._navigation_complete.clear()
        self._navigation_status = "IDLE"

        # Create goal pose
        goal_pose = PoseStamped()
        goal_pose.header.frame_id = "map"
        goal_pose.header.stamp = self.navigator.get_clock().now().to_msg()
        goal_pose.pose.position.x = x
        goal_pose.pose.position.y = y
        goal_pose.pose.position.z = 0.0

        # Convert theta to quaternion
        goal_pose.pose.orientation.x = 0.0
        goal_pose.pose.orientation.y = 0.0
        goal_pose.pose.orientation.z = math.sin(theta / 2.0)
        goal_pose.pose.orientation.w = math.cos(theta / 2.0)

        self.logger.debug(f"Planning path to: x={x}, y={y}, theta={theta}")

        # Get the global path from Nav2
        path = self.navigator.getPath(goal_pose, goal_pose, use_start=False)

        if path is None:
            self.logger.error("Failed to get path to goal")
            return "FAILED"

        self.logger.info(f"Generated path with {len(path.poses)} waypoints")

        # Send the path and goal to the simulator
        self.path_publisher.publish(path)

        self.logger.debug("Path sent to simulator")

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


class NavigateToPositionSim(Primitive):
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
            "Can be used to navigate to a specific point in the map. "
            "This version uses Nav2 for path planning but sends the plan to the simulator for execution."
        )

    def execute(self, x: float, y: float, theta: float):
        self.logger.info(
            f"Planning and executing navigation to position: x={x}, y={y}, theta={theta}"
        )

        result = self.path_controller.go_to_position(x, y, theta)

        # Process result
        if result == "CANCELED":
            self.logger.info("Navigation was canceled")
            return "Navigation canceled", PrimitiveResult.CANCELLED
        elif result == "SUCCEEDED":
            self.logger.info(
                f"Navigation complete. Arrived at position: x={x}, y={y}, theta={theta}"
            )
            return f"Reached position ({x}, {y}, {theta})", PrimitiveResult.SUCCESS
        else:
            self.logger.info(f"Navigation failed with result: {result}")
            return f"Navigation failed with result: {result}", PrimitiveResult.FAILURE

    def cancel(self):
        """Cancels the current navigation task."""
        self.logger.debug("Canceling navigation task")
        self.path_controller.cancel_navigation()
        return "Navigation canceled"
