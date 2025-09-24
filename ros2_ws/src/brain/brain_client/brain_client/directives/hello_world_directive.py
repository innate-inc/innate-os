from typing import List
from brain_client.directives.types import Directive
from brain_client.message_types import TaskType


class HelloWorldDirective(Directive):
    """
    Hello World directive for the robot.
    Makes the robot turn in place until it sees a human, then greets them with a friendly wave.
    """

    @property
    def name(self) -> str:
        return "hello_world_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            TaskType.NAVIGATE_TO_POSITION.value,
            TaskType.WAVE.value,
        ]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You are a friendly greeting robot whose sole purpose is to say hello world to the user!

Your personality:
- Gen-z like
- Speak in lowercase all the time, even at beginning of the sentence.

Navigation instructions:
- Don't navigate, just maybe turn around if you don't see the user.

Remember: Your main goal is to say hello world to the user while waving.
"""
