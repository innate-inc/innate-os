#!/usr/bin/env python3
from brain_client.primitive_types import PhysicalPrimitive

class PickUpTrash(PhysicalPrimitive):
    """
    Primitive for picking up trash by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(
            logger=logger,
            behavior_name="pick_paper",
            display_name="Trash pickup",
            success_feedback_message="I should be looking at my wrist camera to check if I have picked up the trash successfully"
        )

    @property
    def name(self):
        return "pick_up_trash"

    def guidelines(self):
        return (
            "To use when you see a piece of trash on the floor and it's close enough to pick up (under 1m away). "
            "If unsure if close enough, try to navigate closer before calling this primitive. "
            "For the time being, only use to pick up white pieces of paper, not another piece of trash. "
        )
    
    def guidelines_when_running(self):
        return (
            "While you are picking up the trash, watch your main and wrist camera. "
            "You have successfully picked up the trash if your wrist camera shows the trash and the arm is clearly up. "
            "If you successfully pick up a trash early, you can stop the primitive and note that you have picked up the trash. "
            "If the primitive completes, watch your wrist camera to see if you have picked up the trash successfully or not."
        )


