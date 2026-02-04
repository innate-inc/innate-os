#!/usr/bin/env python3
"""
MobilityInterface - Provides base movement (wheels) control capabilities to skills.

This interface allows skills to:
1. Send velocity commands on the common cmd_vel topic
2. Schedule automatic stop after a specified duration
3. Precise blocking rotation via Nav2
"""

import math

from geometry_msgs.msg import PoseStamped, Twist
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from rclpy.node import Node

from brain_client.logging_config import UniversalLogger


class MobilityInterface:
    """High-level interface for base (wheel) motion.

    Skills should use this instead of directly publishing to cmd_vel.
    """

    def __init__(self, node: Node, logger, cmd_vel_topic: str = "/cmd_vel"):
        self.node = node
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self.cmd_vel_topic = cmd_vel_topic

        self._cmd_vel_pub = self.node.create_publisher(Twist, self.cmd_vel_topic, 10)
        self._stop_timer: object | None = None

        # Nav2 navigator for precise movements
        self._navigator = BasicNavigator(namespace="")
        self._navigator_mapfree = BasicNavigator(namespace="mapfree")

        self.logger.info(f"MobilityInterface initialized with cmd_vel topic: {self.cmd_vel_topic}")

    def _schedule_stop(self, duration: float):
        if duration is None or duration <= 0.0:
            return

        # Cancel any existing timer first
        if self._stop_timer is not None:
            try:
                self._stop_timer.cancel()
            except Exception:
                pass
            self._stop_timer = None

        def _stop_callback():
            try:
                stop_cmd = Twist()
                self._cmd_vel_pub.publish(stop_cmd)
                self.logger.debug("MobilityInterface: stop command published")
            finally:
                if self._stop_timer is not None:
                    try:
                        self._stop_timer.cancel()
                    except Exception:
                        pass
                    self._stop_timer = None

        self._stop_timer = self.node.create_timer(duration, _stop_callback)

    def send_cmd_vel(
        self,
        linear_x: float = 0.0,
        angular_z: float = 0.0,
        duration: float | None = None,
    ) -> None:
        """Publish a Twist on the cmd_vel topic.

        Args:
            linear_x: Linear velocity in m/s.
            angular_z: Angular velocity in rad/s.
            duration: If provided (>0), schedule a stop command after this many seconds.
        """
        twist = Twist()
        twist.linear.x = float(linear_x)
        twist.angular.z = float(angular_z)

        self._cmd_vel_pub.publish(twist)

        self.logger.debug(
            f"MobilityInterface: cmd_vel published (linear_x={linear_x}, angular_z={angular_z}, duration={duration})"
        )

        if duration is not None and duration > 0.0:
            self._schedule_stop(duration)

    def rotate_in_place(self, angular_speed: float, duration: float) -> None:
        """Rotate in place with specified angular speed for a duration (non-blocking).

        Args:
            angular_speed: Angular velocity in rad/s (sign determines direction).
            duration: Duration in seconds, after which a stop command is sent.
        """
        self.send_cmd_vel(linear_x=0.0, angular_z=angular_speed, duration=duration)

    def rotate(self, angle_radians: float) -> bool:
        """Rotate in place by a specific angle using Nav2 (blocking).

        Args:
            angle_radians: Angle to rotate in radians (positive = counter-clockwise).

        Returns:
            bool: True if rotation succeeded, False otherwise.
        """
        goal_pose = PoseStamped()
        goal_pose.header.frame_id = "base_link"
        goal_pose.header.stamp = self._navigator.get_clock().now().to_msg()
        goal_pose.pose.position.x = 0.0
        goal_pose.pose.position.y = 0.0
        goal_pose.pose.position.z = 0.0
        goal_pose.pose.orientation.x = 0.0
        goal_pose.pose.orientation.y = 0.0
        goal_pose.pose.orientation.z = math.sin(angle_radians / 2.0)
        goal_pose.pose.orientation.w = math.cos(angle_radians / 2.0)

        self.logger.info(f"MobilityInterface: rotating {math.degrees(angle_radians):.1f}° via Nav2")

        # Get path to verify it's possible
        path = self._navigator_mapfree.getPath(goal_pose, goal_pose, use_start=False)
        if path is None:
            self.logger.error("MobilityInterface: failed to get rotation path")
            return False

        # Execute rotation (blocking)
        self._navigator.goToPose(goal_pose, behavior_tree="mapfree")

        while not self._navigator.isTaskComplete():
            pass  # Block until done

        result = self._navigator.getResult()
        if result == TaskResult.SUCCEEDED:
            self.logger.info("MobilityInterface: rotation complete")
            return True
        else:
            self.logger.error(f"MobilityInterface: rotation failed with {result}")
            return False
