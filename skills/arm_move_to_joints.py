#!/usr/bin/env python3
"""Move arm to a specific joint-space configuration."""

import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmMoveToJoints(Skill):
    """Move the arm to arbitrary joint positions (6 joints in radians)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "arm_move_to_joints"

    def guidelines(self):
        return (
            "Move the arm to a specific joint-space configuration. "
            "Requires exactly 6 joint angles in radians. "
            "Joint 6 is the gripper. Use arm_get_pose to read current position first if needed."
        )

    def execute(self, joint_positions: list[float], duration: int = 3):
        self._cancelled = False
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        if len(joint_positions) != 6:
            return f"Expected 6 joint positions, got {len(joint_positions)}", SkillResult.FAILURE
        self.logger.info(f"Moving arm to joints {joint_positions} over {duration}s")
        success = self.manipulation.move_to_joint_positions(
            joint_positions=joint_positions, duration=duration, blocking=False
        )
        if not success:
            return "Failed to send joint position command", SkillResult.FAILURE
        start = time.time()
        while time.time() - start < duration:
            if self._cancelled:
                return "Arm motion cancelled", SkillResult.CANCELLED
            time.sleep(0.1)
        return f"Arm moved to joint positions {joint_positions}", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Arm move to joints cancelled"
