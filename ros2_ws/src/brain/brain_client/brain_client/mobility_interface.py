#!/usr/bin/env python3
"""
MobilityInterface - Provides base movement (wheels) control capabilities to primitives.

This interface allows primitives to:
1. Send velocity commands on the common cmd_vel topic
2. Schedule automatic stop after a specified duration
"""

from typing import Optional

from rclpy.node import Node
from geometry_msgs.msg import Twist
from brain_client.logging_config import UniversalLogger


class MobilityInterface:
    """High-level interface for base (wheel) motion.

    Primitives should use this instead of directly publishing to cmd_vel.
    """

    def __init__(self, node: Node, logger, cmd_vel_topic: str = "/cmd_vel"):
        self.node = node
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self.cmd_vel_topic = cmd_vel_topic

        self._cmd_vel_pub = self.node.create_publisher(Twist, self.cmd_vel_topic, 10)
        self._stop_timer: Optional[object] = None

        self.logger.info(
            f"MobilityInterface initialized with cmd_vel topic: {self.cmd_vel_topic}"
        )

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
        duration: Optional[float] = None,
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
        """Rotate in place with specified angular speed for a duration.

        Args:
            angular_speed: Angular velocity in rad/s (sign determines direction).
            duration: Duration in seconds, after which a stop command is sent.
        """
        self.send_cmd_vel(linear_x=0.0, angular_z=angular_speed, duration=duration)
