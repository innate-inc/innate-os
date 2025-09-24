#!/usr/bin/env python3
from brain_client.primitives.types import PhysicalPrimitive

class OpenDoor(PhysicalPrimitive):
    """
    Primitive for opening a door by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(
            logger=logger,
            behavior_name="open_door",
            display_name="Door opening",
            success_feedback_message="I should be monitoring the door opening process to ensure it completes successfully"
        )

    @property
    def name(self):
        return "open_door"

    def guidelines(self):
        return (
            "To use when you need to open a door. "
            "You should be close to the door and positioned appropriately to execute the opening motion. "
            "Make sure the door is clearly visible in your main camera and you are at the correct angle to open it. "
        )
    
    def guidelines_when_running(self):
        return ""
