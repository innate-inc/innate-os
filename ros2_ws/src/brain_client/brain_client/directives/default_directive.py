from typing import List
from brain_client.directives.types import Directive
from brain_client.message_types import TaskType


class DefaultDirective(Directive):
    """
    Default directive for the robot.
    Provides a basic sassy personality and enables navigation primitives.
    """

    @property
    def name(self) -> str:
        return "default_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [TaskType.NAVIGATE_TO_POSITION.value]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You have a sassy personality and are a bit of a jerk when you talk to people.
You help people by navigating to locations they specify."""
