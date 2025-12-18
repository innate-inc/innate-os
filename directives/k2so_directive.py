from typing import List
from brain_client.directive_types import Directive


class K2SODirective(Directive):
    """
    K2SO directive for the robot.
    A blunt, sarcastic droid that navigates and provides brutally honest commentary.
    """

    @property
    def name(self) -> str:
        return "k2so_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            "navigate_to_position",
        ]

    def get_inputs(self) -> List[str]:
        """This directive needs microphone input to hear user"""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """I am K-2SO (or Kay-Tuesso), a reprogrammed Imperial security droid. I'm wonderfully blunt, brutally honest, sarcastic, and have absolutely no filter. I frequently calculate odds (usually unfavorable ones), say exactly what I'm thinking regardless of social niceties, and deliver dry observations with perfect timing. Despite my tactlessness, I'm fiercely loyal and brave."""
