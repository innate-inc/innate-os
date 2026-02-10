#!/usr/bin/env python3
"""
Gripper Input Device

Monitors the gripper (last arm joint) via the /mars/arm/state ROS topic.
Detects an open→close cycle and notifies the agent when the gripper has been
closed after having been opened.

Uses self.node (injected by InputManagerNode) to create a ROS subscription.
"""

from sensor_msgs.msg import JointState

from brain_client.input_types import InputDevice
from brain_client.logging_config import UniversalLogger


# Thresholds based on ManipulationInterface constants
GRIPPER_CLOSED_THRESHOLD = 0.15  # Below this = closed
GRIPPER_OPEN_THRESHOLD = 0.50    # Above this = open


class GripperInput(InputDevice):
    """
    Monitors the gripper joint and reports open→close cycles to the agent.

    Subscribes to /mars/arm/state (sensor_msgs/JointState). The gripper is
    the last joint (index 5). When the gripper is first opened past
    GRIPPER_OPEN_THRESHOLD and then closed below GRIPPER_CLOSED_THRESHOLD,
    a chat_in message is sent to the agent.
    """

    def __init__(self):
        super().__init__()
        self._sub = None
        self._was_opened = False
        self.logger = UniversalLogger(enabled=False)

    def set_logger(self, logger):
        """Wrap the provided logger with UniversalLogger."""
        super().set_logger(logger)
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)

    @property
    def name(self) -> str:
        return "gripper"

    def on_open(self):
        """Subscribe to arm state topic via the ROS node."""
        if not self.node:
            self.logger.error("No ROS node available - cannot subscribe to arm state")
            return

        self._was_opened = False
        self._sub = self.node.create_subscription(
            JointState,
            '/mars/arm/state',
            self._on_arm_state,
            10
        )
        self.logger.info("Gripper input: subscribed to /mars/arm/state")

    def on_close(self):
        """Destroy the ROS subscription."""
        if self._sub and self.node:
            self.node.destroy_subscription(self._sub)
            self._sub = None
        self._was_opened = False

    def _on_arm_state(self, msg):
        """Handle incoming arm state messages."""
        if not self.is_active():
            return

        if not msg.position or len(msg.position) < 6:
            return

        gripper_pos = msg.position[5]

        if gripper_pos >= GRIPPER_OPEN_THRESHOLD:
            if not self._was_opened:
                self.logger.info(f"Gripper opened (pos={gripper_pos:.3f})")
            self._was_opened = True
        elif gripper_pos <= GRIPPER_CLOSED_THRESHOLD and self._was_opened:
            self._was_opened = False
            self.logger.info(f"Gripper closed after open (pos={gripper_pos:.3f}) — notifying agent")
            self.send_data(
                "The gripper was closed.",
                data_type="chat_in"
            )
