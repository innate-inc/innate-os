#!/usr/bin/env python3
"""
Arm Vitals Input Device

Monitors arm servo health via the /mars/arm/status ROS topic and notifies
the agent when a servo failure is detected.

Uses self.node (injected by InputManagerNode) to create a ROS subscription.
"""

from maurice_msgs.msg import ArmStatus

from brain_client.input_types import InputDevice
from brain_client.logging_config import UniversalLogger


class ArmVitalsInput(InputDevice):
    """
    Monitors arm servo health and reports failures to the agent.
    
    Subscribes to /mars/arm/status (maurice_msgs/ArmStatus) which is
    published by the arm node's health monitor. When is_ok transitions
    to False, sends the error description to the agent as a chat_in message.
    """

    def __init__(self):
        super().__init__()
        self._sub = None
        self._last_is_ok = True
        self.logger = UniversalLogger(enabled=False)

    def set_logger(self, logger):
        """Wrap the provided logger with UniversalLogger."""
        super().set_logger(logger)
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)

    @property
    def name(self) -> str:
        return "arm_vitals"

    def on_open(self):
        """Subscribe to arm status topic via the ROS node."""
        if not self.node:
            self.logger.error("No ROS node available - cannot subscribe to arm status")
            return

        self._last_is_ok = True
        self._sub = self.node.create_subscription(
            ArmStatus,
            '/mars/arm/status',
            self._on_arm_status,
            10
        )
        self.logger.info("Subscribed to /mars/arm/status")

    def on_close(self):
        """Destroy the ROS subscription."""
        if self._sub and self.node:
            self.node.destroy_subscription(self._sub)
            self._sub = None
        self._last_is_ok = True

    def _on_arm_status(self, msg):
        """Handle incoming arm status messages."""
        if not self.is_active():
            return

        if not msg.is_ok and self._last_is_ok:
            error_text = f"One of the arm servos failed: {msg.error}"
            self.logger.warning(f"Arm failure detected: {msg.error}")
            self.send_data(error_text, data_type="chat_in")

        self._last_is_ok = msg.is_ok
