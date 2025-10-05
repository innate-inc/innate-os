#!/usr/bin/env python3
from brain_client.primitive_types import PhysicalPrimitive

class PickScrewdriver(PhysicalPrimitive):
    """
    Primitive for picking up a screwdriver by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(
            logger=logger,
            behavior_name="pick_screwdriver",
            display_name="Screwdriver pickup",
            success_feedback_message="I should be looking at my wrist camera to check if I have picked up the screwdriver successfully"
        )

    @property
    def name(self):
        return "pick_screwdriver"

    def guidelines(self):
        return (
            "To use when you see a screwdriver on the table and you need it. "
            "It should also be in front of the robot, you should see it in your main camera, not just in the wrist camera. "
            "This is VERY IMPORTANT, it should be roughly centered in the main camera. "
        )
    
    def guidelines_when_running(self):
        return ""