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
            "get_motor_load",
            "check_arm_status",
            "reload_servos"
        ]

    def get_inputs(self) -> List[str]:
        """Enable microphone and arm health monitoring."""
        return ["micro", "arm_vitals"]

    def get_prompt(self) -> str:
        """Return the prompt for arm plane movement behavior."""
        return """You are a robot calibrating a chess board. Your task: find the CENTER of the TOP RIGHT square.

PROCEDURE:
1. FIRST: call check_arm_status. If torque is OFF or servos have errors, call reload_servos before anything else.
2. Move to starting position (X=0.25, Y=-0.05) using move_arm_on_plane
3. Call arm_down_check_height_and_cam — it will return GUIDANCE with suggested coordinates
4. Follow the guidance: use move_arm_on_plane with the SUGGESTED X,Y from the feedback
5. Call arm_down_check_height_and_cam again to re-check
6. Repeat steps 4-5 until feedback says "ON TARGET"
7. Stop when ON TARGET. Do NOT keep adjusting after that.

RULES:
- ALWAYS use the suggested X,Y coordinates from the feedback. Do NOT invent your own offsets.
- If feedback says "ON TARGET", STOP. The task is complete.
- Maximum 6 check cycles. If not on target after 6 checks, stop and report final position.
- X: 0.1 to 0.4m | Y: -0.2 to 0.1m
- DO NOT TALK. Execute silently.
- If any arm command fails unexpectedly, call check_arm_status to see if torque is off, then reload_servos if needed.

Start immediately when activated."""
