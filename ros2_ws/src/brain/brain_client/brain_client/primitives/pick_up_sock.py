#!/usr/bin/env python3
from brain_client.primitive_types import PhysicalPrimitive

class PickUpSock(PhysicalPrimitive):
    """
    Primitive for picking up a sock by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(
            logger=logger,
            behavior_name="pick_socks",
            display_name="Sock pickup",
            success_feedback_message="I should be looking at my wrist camera to check if I have picked up the sock successfully"
        )

    @property
    def name(self):
        return "pick_up_sock"

    def guidelines(self):
        return (
            "To use when you see a sock on the floor and it's close enough to pick up (under like 70cm away). "
            "It should also be in front of the robot, make sure you are facing the sock by checking your angle to it! "
            "This is VERY IMPORTANT: The sock should be CENTERED in your main camera, check your angle to it beforehand! "
            "After trying, if it failed because the arm missed the sock, you can suggest to turn a little bit to the left or right to attempt a better pick up after the first time it failed."
            "Try like 25 degrees to the left or right depending on which way the arm failed."
        )
    
    def guidelines_when_running(self):
        return (
            "While you are picking up the sock, watch your main and wrist camera. "
            "You have successfully picked up the sock if your wrist camera shows the sock and the arm is clearly up. "
            "If you successfully pick up a sock early, you can stop the primitive and note that you have picked up the sock. But really ONLY IF YOU SEE THE SOCK IN THE AIR. "
            "If the primitive completes, watch your wrist camera to see if you have picked up the sock successfully or not."
        )

 