#!/usr/bin/env python3
from brain_client.primitive_types import PhysicalPrimitive

class DropSocks(PhysicalPrimitive):
    """
    Primitive for dropping a sock by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(
            logger=logger,
            behavior_name="drop_socks",
            display_name="Sock dropping",
            success_feedback_message="I should be looking at my wrist camera to check if I have dropped the sock successfully"
        )

    @property
    def name(self):
        return "drop_socks"

    def guidelines(self):
        return (
            "To use when you are holding a sock and want to drop it in the wooden box associated for it. "
            "The wooden box HAS to be visible in your main camera. Do NOT drop the sock if you don't see the wooden box directly from the main camera!"
        )

    def guidelines_when_running(self):
        return (
            "While you are dropping the sock, watch your main and wrist camera. "
            "You have successfully dropped the sock if your wrist camera shows your gripper is empty and the sock is in the wooden box. "
            "If you successfully drop a sock in the wooden box, you can stop the primitive. "
            "If the primitive completes, watch your wrist camera to see if you have dropped the sock successfully or not."
        )

 