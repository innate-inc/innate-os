from typing import List
from brain_client.agent_types import Agent


class BasicAgent(Agent):
    """
    Default directive for the robot.
    Provides a basic professional personality and enables navigation primitives.
    """

    @property
    def id(self) -> str:
        return "basic_agent"

    @property
    def display_name(self) -> str:
        return "No Prompt"

    def get_skills(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            "navigate_to_position",
        ]

    def get_inputs(self) -> List[str]:
        """Enable microphone input to hear user"""
        return ["micro"]

    def get_prompt(self) -> None:
        return None
