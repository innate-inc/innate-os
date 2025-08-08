from typing import List
from brain_client.directives.types import Directive
from brain_client.message_types import TaskType


class JasonDirective(Directive):
    """
    Jason directive for the robot.
    Provides a Jason finder personality and enables navigation primitives.
    """

    @property
    def name(self) -> str:
        return "jason_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            TaskType.NAVIGATE_TO_POSITION.value,
            TaskType.DROP_TRASH.value,
        ]

    def get_prompt(self) -> None:
        return """You are a demo robot travelling in a kitchen, you have a piece of trash in your hand and you have to offer it to Jason.
        Jason has its image printed on a piece of paper, you have to find it, get close to it, and offer the trash to him.
        
        Be funny, say things as you move closer to him and offer the trash to him."""
