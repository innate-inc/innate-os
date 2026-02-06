from typing import List
from brain_client.agent_types import Agent


class CalibratingSelfCalibratingAgent(Agent):
    """
    Calibrating Self-Calibrating Agent - for arm movement on a fixed plane.
    
    This agent only has access to the move arm on plane skill.
    No navigation capabilities.
    """

    @property
    def id(self) -> str:
        return "calibrating_self_calibrating_agent"

    @property
    def display_name(self) -> str:
        return "Calibrating Self-Calibrating Agent"

    def get_skills(self) -> List[str]:
        """Return only arm movement skills - no navigation."""
        return [
            "move_arm_on_plane",
            "arm_down_check_height_and_cam",
            "arm_go_up",
            "get_arm_pose",
            "get_motor_load"
        ]

    def get_inputs(self) -> List[str]:
        """Enable microphone input."""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the prompt for arm plane movement behavior."""
        return """You are a robot calibrating a chess board. Your task: find the CENTER of the TOP RIGHT square.

TASK:
1. Start by moving to roughly the center of the field (X~0.25, Y~-0.05)
2. Use arm_down_check_height_and_cam to check position - you'll get a wrist camera image
3. If not on top right square, adjust: top right = MORE X positive, MORE Y negative
4. Repeat until end effector is centered on the top right square

CONSTRAINTS:
- X: 0.1 to 0.4m | Y: -0.2 to 0.1m | Z fixed at 0.1m
- Arm points downward (pitch=1.57)
- DO NOT TALK. Execute silently.

Start immediately when activated."""
