#!/usr/bin/env python3
"""
Arm Move To XYZ Skill - Move arm to a Cartesian position using IK.
"""

import time
from brain_client.skill_types import Skill, SkillResult


class ArmMoveToXYZ(Skill):
    """Move the arm to a Cartesian position using inverse kinematics."""
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "arm_move_to_xyz"
    
    def guidelines(self):
        return (
            "Move the arm end-effector to a target position in Cartesian space (x, y, z in meters). "
            "Coordinates are relative to the robot base_link. Optionally specify roll, pitch, yaw orientation in radians."
        )
    
    def execute(
        self,
        x: float,
        y: float,
        z: float,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        duration: int = 3
    ):
        """
        Move arm to Cartesian pose using IK.
        
        Args:
            x: Target x position in meters (forward from base)
            y: Target y position in meters (left from base)
            z: Target z position in meters (up from base)
            roll: Target roll orientation in radians
            pitch: Target pitch orientation in radians
            yaw: Target yaw orientation in radians
            duration: Motion duration in seconds
        """
        self._cancelled = False
        
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        self.logger.info(
            f"Moving arm to XYZ ({x}, {y}, {z}) with RPY ({roll}, {pitch}, {yaw}) over {duration}s"
        )
        
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=z,
            roll=roll, pitch=pitch, yaw=yaw,
            duration=duration
        )
        
        if not success:
            return "Failed to solve IK or send arm command", SkillResult.FAILURE
        
        # Wait for motion to complete (with cancellation check)
        start_time = time.time()
        while time.time() - start_time < duration:
            if self._cancelled:
                return "Arm motion cancelled", SkillResult.CANCELLED
            time.sleep(0.1)
        
        return f"Arm moved to ({x}, {y}, {z})", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
