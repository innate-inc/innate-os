#!/usr/bin/env python3
"""Disable torque on all arm motors."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmTorqueOff(Skill):
    """Disable torque on all arm servos — arm goes limp."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_torque_off"

    def guidelines(self):
        return (
            "Disable torque on all arm motors. The arm will go limp and "
            "can be manually positioned. Useful for kinesthetic teaching "
            "or when the arm is not needed."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        self.logger.info("Disabling arm torque")
        success = self.manipulation.torque_off()
        if not success:
            return "Failed to disable torque", SkillResult.FAILURE
        return "Arm torque disabled", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel"
