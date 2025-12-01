#!/usr/bin/env python3
"""
HeadInterface - Provides head tilt control capabilities to primitives.

This interface allows primitives to:
1. Set the head tilt position
2. Access current head position from robot state
"""

from rclpy.node import Node
from std_msgs.msg import Int32


class HeadInterface:
    """High-level interface for head (tilt) control.

    Primitives should use this instead of directly publishing to head topics.
    """

    def __init__(self, node: Node, logger, head_position_topic: str = "/mars/head/set_position"):
        self.node = node
        self.logger = logger
        self.head_position_topic = head_position_topic

        # Publisher for head position commands
        self._head_position_pub = self.node.create_publisher(
            Int32, self.head_position_topic, 10
        )

        self.logger.info(
            f"HeadInterface initialized with position topic: {self.head_position_topic}"
        )

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

        if self.logger:
            self.logger.debug(
                f"HeadInterface: set head position to {angle_degrees} degrees"
            )
