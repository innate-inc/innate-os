from brain_client.agents.types import Agent


class DefaultAgent(Agent):
    """
    Default directive for the robot.
    Provides a simple professional personality and enables navigation primitives.
    """

    @property
    def id(self) -> str:
        return "default_agent"

    @property
    def display_name(self) -> str:
        return "No Prompt"

    def get_skills(self) -> list[str]:
        """Return the list of skill IDs this directive can use"""
        return ["innate-os/navigate_to_position", "innate-os/navigate_with_vision"]

    def get_inputs(self) -> list[str]:
        """Enable microphone input to hear user"""
        return ["micro"]

    def get_prompt(self) -> None:
        return None
