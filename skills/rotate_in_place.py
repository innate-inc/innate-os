#!/usr/bin/env python3
"""Rotate the robot in place at a given angular speed for a duration."""

import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class RotateInPlace(Skill):
    """Rotate in place with specified angular speed for a duration."""

    mobility = Interface(InterfaceType.MOBILITY)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "rotate_in_place"

    def guidelines(self):
        return (
            "Rotate the robot in place at a given angular speed for a duration. "
            "Positive = counter-clockwise, negative = clockwise. "
            "For precise angle-based rotation, use rotate_angle instead."
        )

    def execute(self, angular_speed: float, duration: float = 1.0):
        self._cancelled = False

        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE

        self.logger.info(f"Rotating in place at {angular_speed} rad/s for {duration}s")

        self.mobility.rotate_in_place(angular_speed=angular_speed, duration=duration)

        start = time.time()
        while time.time() - start < duration:
            if self._cancelled:
                self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
                return "Rotation cancelled", SkillResult.CANCELLED
            time.sleep(0.1)

        return f"Rotated at {angular_speed} rad/s for {duration}s", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Rotate in place cancelled"
