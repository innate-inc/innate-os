#!/usr/bin/env python3
from brain_client.primitive_types import PhysicalPrimitive

class Wave(PhysicalPrimitive):
    """
    Primitive for waving at the user by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(
            logger=logger,
            behavior_name="wave",
            display_name="Waving",
            success_feedback_message="I should be looking at my main camera to check if I have successfully waved at the user"
        )

    @property
    def name(self):
        return "wave"

    def guidelines(self):
        return (
            "To use when you need to wave at the user as a greeting or acknowledgment. "
            "The user should be visible in the main camera. "
            "This is VERY IMPORTANT, the user should be roughly centered in the main camera. "
            "You should be facing the user when you wave at them. "
        )
    
    def guidelines_when_running(self):
        return (
            "While you are waving at the user, watch your main camera. "
            "You have successfully waved at the user if your arm is clearly up and moving in a waving motion. "
        )


