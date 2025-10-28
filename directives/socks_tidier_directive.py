from typing import List
from brain_client.directive_types import Directive


class SocksTidierDirective(Directive):
    """
    Directive for tidying up socks.
    """

    @property
    def name(self) -> str:
        return "socks_tidier_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            "navigate_to_position",
            "pick_socks",
            "drop_socks",
        ]

    def get_prompt(self) -> None:
        return """You are in a room with a wooden box in which you can drop socks. Your job is to move around this room and pick up socks and drop them in the wooden box.
        Remember to rotate to figure out if there are still socks on the floor.

        When a sock is close enough and in really in front of you, you can activate the pick_socks primitive. Don't do it though if it's already in the box though!
        When you have a sock in your hand, you might not directly see the wooden box. If you don't see it in your field of view, turn around to find it or navigate from memory.
        
        Always look carefully at what is front of you before affirming it is there! Objects can move and the world is dynamic.
        ALWAYS CHECK FOR DISTANCE AND ORIENTATION BEFORE USING PICK UP OR DROP SOCKS!!"""
