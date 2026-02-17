#!/usr/bin/env python3
"""
Arm Utils Skill - Torque on, torque off, or reboot the arm servos.
"""

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


VALID_COMMANDS = ("torque_on", "torque_off", "reboot_arm")


class ArmUtils(Skill):
    """Utility commands for the arm: enable/disable torque or reboot servos."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_utils"

    def guidelines(self):
        return (
            "Utility skill for low-level arm commands. "
            "Requires 'command' parameter: 'torque_on', 'torque_off', or 'reboot_arm'. "
            "torque_on enables motor torque so the arm holds position. "
            "torque_off disables torque so the arm goes limp (for manual positioning). "
            "reboot_arm reboots all Dynamixel servos to clear hardware errors."
        )

    def execute(self, command: str):
        """
        Execute an arm utility command.

        Args:
            command: 'torque_on', 'torque_off', or 'reboot_arm'
        """
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        command = command.strip().lower()
        if command not in VALID_COMMANDS:
            return (
                f"Invalid command '{command}'. Must be one of: {', '.join(VALID_COMMANDS)}.",
                SkillResult.FAILURE,
            )

        if command == "torque_on":
            success = self.manipulation.torque_on()
            if success:
                return "Arm torque enabled", SkillResult.SUCCESS
            return "Failed to enable arm torque", SkillResult.FAILURE

        if command == "torque_off":
            success = self.manipulation.torque_off()
            if success:
                return "Arm torque disabled (arm is limp)", SkillResult.SUCCESS
            return "Failed to disable arm torque", SkillResult.FAILURE

        # reboot_arm
        success = self.manipulation.reboot_servos()
        if success:
            return "Arm servos rebooted and reinitialized", SkillResult.SUCCESS
        return "Failed to reboot arm servos", SkillResult.FAILURE

    def cancel(self):
        return "Arm utils cannot be cancelled"
