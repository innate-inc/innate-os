#!/usr/bin/env python3
"""Reboot all arm Dynamixel servos."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmRebootServos(Skill):
    """Reboot all Dynamixel servos to clear hardware errors."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_reboot_servos"

    def guidelines(self):
        return (
            "Reboot all arm Dynamixel servos. Clears hardware errors and "
            "reinitializes motor control. Use when servos are in error state "
            "and not responding to commands."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        self.logger.info("Rebooting arm servos")
        success = self.manipulation.reboot_servos()
        if not success:
            return "Failed to reboot servos", SkillResult.FAILURE
        return "Arm servos rebooted", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel"
