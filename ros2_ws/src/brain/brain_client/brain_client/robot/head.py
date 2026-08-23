#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Head - Provides head tilt control capabilities to skills.

This interface allows skills to:
1. Set the head tilt position
2. Access current head position from robot state
"""

from rclpy.node import Node
from std_msgs.msg import Int32

from brain_client.common.logging import UniversalLogger


class Head:
    """High-level interface for head (tilt) control.

    Injected as ``self.head`` when a skill declares ``head: Head``; the
    framework constructs it. Skills should use this instead of directly
    publishing to head topics.
    """

    def __init__(self, node: Node, logger, head_position_topic: str = "/mars/head/set_position"):
        self.node = node
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self.head_position_topic = head_position_topic

        # Publisher for head position commands
        self._head_position_pub = self.node.create_publisher(Int32, self.head_position_topic, 10)

        self.logger.info(f"Head initialized with position topic: {self.head_position_topic}")

    def set_position(self, angle_degrees: int) -> None:
        """Set the head tilt position.

        Args:
            angle_degrees: Target angle in degrees (integer).
                          Typical range: -25 to +15 degrees
                          - Negative = looking down
                          - Positive = looking up
        """
        if not isinstance(angle_degrees, int):
            angle_degrees = int(angle_degrees)

        msg = Int32()
        msg.data = angle_degrees

        self._head_position_pub.publish(msg)

        self.logger.debug(f"Head: set head position to {angle_degrees} degrees")

    def close(self) -> None:
        self.node.destroy_publisher(self._head_position_pub)
