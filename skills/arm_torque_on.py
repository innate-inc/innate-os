#!/usr/bin/env python3
"""Enable torque on all arm motors."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmTorqueOn(Skill):
    """Enable torque on all arm servos so the arm holds its position."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_torque_on"

    def guidelines(self):
        return (
            "Enable torque on all arm motors. The arm will hold its current "
            "position. Use after arm_torque_off to re-engage the arm."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        self.logger.info("Enabling arm torque")
        success = self.manipulation.torque_on()
        if not success:
            return "Failed to enable torque", SkillResult.FAILURE
        return "Arm torque enabled", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel"
