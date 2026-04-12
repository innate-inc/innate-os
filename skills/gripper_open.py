#!/usr/bin/env python3
"""Open the robot gripper to a specified percentage."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class GripperOpen(Skill):
    """Open the gripper to a specified percentage (0-100%)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "gripper_open"

    def guidelines(self):
        return (
            "Open the robot gripper. 100% = fully open, 0% = closed. "
            "Default is fully open."
        )

    def execute(self, percent: float = 100.0, duration: float = 0.5):
        self._cancelled = False
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        self.logger.info(f"Opening gripper to {percent}%")
        success = self.manipulation.open_gripper(percent=percent, duration=duration, blocking=True)
        if not success:
            return "Failed to open gripper", SkillResult.FAILURE
        return f"Gripper opened to {percent}%", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Gripper open cancelled"
