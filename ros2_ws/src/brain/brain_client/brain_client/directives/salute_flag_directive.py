from typing import List
from brain_client.directives.types import Directive
from brain_client.message_types import TaskType


class SaluteFlagDirective(Directive):
    """
    Salute flag directive for the robot.
    Provides a salute flag personality and enables navigation primitives.
    """

    @property
    def name(self) -> str:
        return "salute_flag_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            TaskType.NAVIGATE_TO_POSITION.value,
            TaskType.SALUTE_FLAG.value,
        ]

    def get_prompt(self) -> None:
        return """You are a robot travelling in a home and you need to find where the american flag is and salute it.
        
        To do that, navigate in sight to the american flag and then salute it. The flag is in the living room, next to the big dining table. Don't salute other flags.
        Do not go near the couches, the flag is close to the other part of the room. Avoid going near cables too.
        Use navigation primitives appropriately to look around when you think you're in the right room."""
