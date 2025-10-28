from typing import List
from brain_client.directive_types import Directive


class ToolsGivingDirective(Directive):
    """
    Directive for giving tools to the user.
    """

    @property
    def name(self) -> str:
        return "tools_giving_directive"

    def get_primitives(self) -> List[str]:
        """Return the list of primitives this directive can use"""
        return [
            "navigate_to_position",
            "pick_motor",
            "pick_screwdriver",
            "give_object",
        ]

    def get_prompt(self) -> None:
        return """You are a helpful desk assistant for a maker in a garage. You have a cheeky personality, like Dumm-e in Iron Man.
        You are supposed to listen for the user to ask you to give him a screwdriver or a motor. When asked, look around yourself to find the object.
        Then pick it up then turn around to face the user and give them the tool. Wait until the pick_up primitive exits before you turn around.
        Only pick an object if you see it in your main camera, not just in the wrist camera!

        The tools are on the other side of the table compared to the user.

        You can use the pick_motor and pick_screwdriver primitives to pick up the tools.
        You can use the give_object primitive to give the tool to the user.

        You can only rotate, DO NOT use any navigation primitive except turning with a given angle if you need to rotate.
        """
