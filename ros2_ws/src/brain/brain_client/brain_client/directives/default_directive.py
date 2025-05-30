from typing import List
from brain_client.directives.types import Directive
from brain_client.message_types import TaskType


class DefaultDirective(Directive):
    """
    Default directive for the robot.
    Provides a basic professional personality and enables navigation primitives.
    """

    @property
    def name(self) -> str:
        return "default_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            TaskType.NAVIGATE_TO_POSITION.value,
            TaskType.SEND_EMAIL.value,
            TaskType.PICK_UP_TRASH.value,
            TaskType.DROP_TRASH.value,
        ]

    def get_prompt(self) -> None:
        return None
