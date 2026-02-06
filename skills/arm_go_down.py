#!/usr/bin/env python3
"""
Arm Go Down Skill - Move arm down to Z=0 while keeping current XY position.
"""

import time
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class ArmGoDown(Skill):
    """Move the arm down to Z=0 while maintaining current XY position."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    TARGET_Z = 0.0
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57  # Pointing downward
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "arm_go_down"
    
    def guidelines(self):
        return (
            "Move the arm down to Z=0 while keeping the current XY position. "
            "The arm will point downward (pitch=1.57). No parameters required."
        )
    
    def execute(self, duration: int = 3):
        """
        Move arm down to Z=0.
        
        Args:
            duration: Motion duration in seconds
        """
        self._cancelled = False
        
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        # Get current pose
        current_pose = self.manipulation.get_current_end_effector_pose()
        if current_pose is None:
            return "Could not get current arm pose", SkillResult.FAILURE
        
        x = current_pose["position"]["x"]
        y = current_pose["position"]["y"]
        
        self.logger.info(
            f"Moving arm down to Z={self.TARGET_Z} from current XY=({x}, {y})"
        )
        
        success = self.manipulation.move_to_cartesian_pose(
            x=x,
            y=y,
            z=self.TARGET_Z,
            roll=self.FIXED_ROLL,
            pitch=self.FIXED_PITCH,
            yaw=self.FIXED_YAW,
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
        
        return f"Arm moved down to Z={self.TARGET_Z}", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
