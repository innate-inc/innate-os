#!/usr/bin/env python3
from brain_client.primitives.types import PhysicalPrimitive

class GiveObject(PhysicalPrimitive):
    """
    Primitive for giving an object to the user by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(
            logger=logger,
            behavior_name="give_object",
            display_name="Object giving",
            success_feedback_message="I should be looking at my wrist camera to check if I have given the object to the user successfully"
        )

    @property
    def name(self):
        return "give_object"

    def guidelines(self):
        return (
            "To use when you have an object in your hand and you need to give it to the user. "
            "It should also be in front of the robot. "
            "This is VERY IMPORTANT, it should be roughly centered in the main camera. "
            "You should be facing the user when you give the object to them. "
        )
    
    def guidelines_when_running(self):
        return (
            "While you are giving the object to the user, watch your main and wrist camera. "
            "You have successfully given the object to the user if your wrist camera shows the object and the arm is clearly up. "
        )

 