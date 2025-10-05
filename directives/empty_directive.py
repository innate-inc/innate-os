from typing import List
from brain_client.directives.types import Directive


class EmptyDirective(Directive):
    """
    Default directive for the robot.
    Provides a basic professional personality and enables navigation primitives.
    """

    @property
    def name(self) -> str:
        return "empty_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            "navigate_to_position",
        ]

    def get_prompt(self) -> None:
        return None
