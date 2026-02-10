#!/usr/bin/env python3
"""
Drive Cmd Vel Skill - Send a velocity command for a given duration.
"""

import time
from brain_client.mobility_interface import MobilityInterface
from brain_client.skill_types import Skill, SkillResult, Interface


class DriveCmdVel(Skill):
    """Send a cmd_vel command (linear + angular) for a specified duration."""

    mobility = Interface(MobilityInterface)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "drive_cmd_vel"

    def guidelines(self):
        return (
            "Drive the robot base with a velocity command. "
            "Parameters: 'linear_x' (m/s, default 0), 'angular_z' (rad/s, default 0), "
            "'duration' (seconds, default 1). "
            "Sends the command and waits for the duration before returning."
        )

    def execute(self, linear_x: float = 0.0, angular_z: float = 0.0, duration: float = 1.0):
        """
        Send a cmd_vel command for a given duration.

        Args:
            linear_x: Linear velocity in m/s (positive = forward)
            angular_z: Angular velocity in rad/s (positive = counter-clockwise)
            duration: How long to drive in seconds
        """
        self._cancelled = False

        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE

        if duration <= 0.0:
            return "Duration must be positive", SkillResult.FAILURE

        self.logger.info(
            f"[DriveCmdVel] Sending cmd_vel: linear_x={linear_x}, angular_z={angular_z}, duration={duration}s"
        )
        self._send_feedback(f"Driving: linear={linear_x} m/s, angular={angular_z} rad/s for {duration}s")

        self.mobility.send_cmd_vel(linear_x=linear_x, angular_z=angular_z, duration=duration)

        # Wait for the duration (with cancellation check)
        start_time = time.time()
        while time.time() - start_time < duration:
            if self._cancelled:
                self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
                return "Drive cancelled", SkillResult.CANCELLED
            time.sleep(0.1)

        self.logger.info("[DriveCmdVel] Complete")
        return f"Drove for {duration}s (linear={linear_x}, angular={angular_z})", SkillResult.SUCCESS

    def cancel(self):
        """Cancel the drive command and stop."""
        self._cancelled = True
        if self.mobility is not None:
            self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
        return "Drive cancelled"
