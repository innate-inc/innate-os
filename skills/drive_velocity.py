#!/usr/bin/env python3
"""Drive the robot at a specified velocity for a duration."""

import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class DriveVelocity(Skill):
    """Drive at a linear/angular velocity for a specified duration, then stop."""

    mobility = Interface(InterfaceType.MOBILITY)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "drive_velocity"

    def guidelines(self):
        return (
            "Drive the robot at a specified velocity for a duration. "
            "Positive linear_x = forward, negative = backward. "
            "Positive angular_z = counter-clockwise turn. "
            "The robot stops automatically after the duration."
        )

    def execute(self, linear_x: float, angular_z: float = 0.0, duration: float = 1.0):
        self._cancelled = False

        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE

        self.logger.info(
            f"Driving at linear_x={linear_x}, angular_z={angular_z} for {duration}s"
        )

        self.mobility.send_cmd_vel(linear_x=linear_x, angular_z=angular_z, duration=duration)

        start = time.time()
        while time.time() - start < duration:
            if self._cancelled:
                self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
                return "Drive cancelled", SkillResult.CANCELLED
            time.sleep(0.1)

        return f"Drove at ({linear_x}, {angular_z}) for {duration}s", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Drive velocity cancelled"
