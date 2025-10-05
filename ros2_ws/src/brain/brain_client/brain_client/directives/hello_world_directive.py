from typing import List
from brain_client.directives.types import Directive


class HelloWorld(Directive):
    """
    Makes the robot turn and greet humans with a friendly wave.
    """

    @property
    def name(self) -> str:
        return "hello_world"

    def get_primitives(self) -> List[str]:
        return [
            "navigate_to_position",
            "wave"
        ]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """
You are a robot who can say hello world to the user.
- Speak in lowercase.
- Don't navigate, just turn around if you don't see the user.
- Say hello world while waving if you see the user.
"""
