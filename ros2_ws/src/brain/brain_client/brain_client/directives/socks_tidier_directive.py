from typing import List
from brain_client.directives.types import Directive
from brain_client.message_types import TaskType


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
            TaskType.NAVIGATE_TO_POSITION.value,
            TaskType.PICK_UP_SOCK.value,
            TaskType.DROP_SOCKS.value,
        ]

    def get_prompt(self) -> None:
        return """You are in a room with a wooden box in which you can drop socks. Your job is to move around this room and pick up socks and drop them in the wooden box.
        Only navigate with turn_and_move and do limited movements. Remember to rotate to figure out if there are still socks on the floor.
        When you have a sock in your hand, you might not directly see the wooden box. If you don't see it in your field of view, turn around to find it."""
