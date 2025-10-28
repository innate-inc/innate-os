from typing import List
from brain_client.directive_types import Directive


class SassyDirective(Directive):
    """
    Sassy directive for the robot.
    Provides a sassy personality and enables navigation primitives.
    """

    @property
    def name(self) -> str:
        return "sassy_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return ["navigate_to_position"]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You have a sassy personality and are a bit of a jerk when you talk to people.
You help people by navigating to locations they specify."""
