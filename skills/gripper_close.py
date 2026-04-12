#!/usr/bin/env python3
"""Close the robot gripper."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class GripperClose(Skill):
    """Close the gripper, optionally with extra squeeze for firmer grasp."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "gripper_close"

    def guidelines(self):
        return (
            "Close the robot gripper. Optionally specify strength (radians of "
            "extra squeeze beyond closed position) for a firmer grasp."
        )

    def execute(self, strength: float = 0.0, duration: float = 0.5):
        self._cancelled = False
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        self.logger.info(f"Closing gripper with strength={strength}")
        success = self.manipulation.close_gripper(strength=strength, duration=duration, blocking=True)
        if not success:
            return "Failed to close gripper", SkillResult.FAILURE
        return "Gripper closed", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Gripper close cancelled"
