#!/usr/bin/env python3
"""Rotate the robot by a precise angle using Nav2."""

import math

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class RotateAngle(Skill):
    """Rotate in place by a specific angle using Nav2 (blocking)."""

    mobility = Interface(InterfaceType.MOBILITY)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "rotate_angle"

    def guidelines(self):
        return (
            "Rotate the robot by a precise angle using Nav2. "
            "Positive = counter-clockwise. This is blocking and precise — "
            "use for known angles. For continuous spinning, use rotate_in_place instead."
        )

    def execute(self, angle_radians: float):
        self._cancelled = False

        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE

        self.logger.info(f"Rotating {math.degrees(angle_radians):.1f}° via Nav2")

        success = self.mobility.rotate(angle_radians)

        if not success:
            return "Rotation failed", SkillResult.FAILURE

        return f"Rotated {math.degrees(angle_radians):.1f}°", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Rotate angle cancelled"
