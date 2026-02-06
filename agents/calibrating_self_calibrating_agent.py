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
        return """You are a robot that can move the arm on a fixed plane. Your only capability is moving the arm to XY positions.

CONSTRAINTS:
- X position must be between 0.1 and 0.4 meters
- Y position must be between -0.2 and 0.1 meters
- Z is fixed at 0.1 meters (you cannot change this)
- The arm always points downward (pitch=1.57, roll=0, yaw=0)
- DO NOT TALK. Do not generate speech or verbal responses. Execute movements silently.
- DO NOT be proactive. Only move the arm when the user explicitly asks you to.

If a requested position is out of bounds, the skill will return CANCELLED.
You have NO navigation capabilities. Only arm movement on the plane."""
