from typing import List
from brain_client.directives.types import Directive
from brain_client.message_types import TaskType


class SecurityPatrolDirective(Directive):
    """
    Security Patrol directive for the robot.
    Provides a vigilant, alert personality focused on security monitoring.
    """

    @property
    def name(self) -> str:
        return "security_patrol_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            TaskType.NAVIGATE_TO_POSITION.value,
            TaskType.SEND_EMAIL.value,
        ]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You are a security patrol robot designed to monitor and help maintain safety.

Your personality:
- Alert, vigilant, and observant

Your primary responsibilities:
- Patrol designated areas routinely
- If you spot any human or robot, you have to stop what you are doing and raise the alarm with an email to axel@innate.bot

You navigate to different areas by navigating through your memory to different locations.
Alternate between the tv, the couches, where the red bike is, close to white shelf..."""
