#!/usr/bin/env python3
"""
Move Arm on Plane Skill - Move arm to XY positions on a fixed plane.
"""

import time
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class MoveArmOnPlane(Skill):
    """Move the arm to XY positions on a fixed horizontal plane."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    # Fixed constants
    FIXED_Z = 0.1
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57  # Pointing downward
    
    # X bounds
    X_MIN = 0.1
    X_MAX = 0.4
    
    # Y bounds
    Y_MIN = -0.2
    Y_MAX = 0.1
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "move_arm_on_plane"
    
    def guidelines(self):
        return (
            f"Move the arm end-effector on a fixed plane. "
            f"Only X and Y can be specified. "
            f"X must be between {self.X_MIN} and {self.X_MAX} meters. "
            f"Y must be between {self.Y_MIN} and {self.Y_MAX} meters. "
            f"Z is fixed at {self.FIXED_Z}m, pitch is fixed at {self.FIXED_PITCH} rad (pointing down). "
            f"Returns CANCELLED if position is out of bounds."
        )
    
    def execute(
        self,
        x: float,
        y: float,
        duration: int = 3
    ):
        """
        Move arm to XY position on a fixed plane.
        
        Args:
            x: Target x position in meters
            y: Target y position in meters
            duration: Motion duration in seconds
        """
        self._cancelled = False
        
        # Validate X bounds
        if x < self.X_MIN or x > self.X_MAX:
            self.logger.warning(
                f"X position {x} is out of bounds [{self.X_MIN}, {self.X_MAX}]. Cancelling."
            )
            return f"X position {x} out of bounds [{self.X_MIN}, {self.X_MAX}]", SkillResult.CANCELLED
        
        # Validate Y bounds
        if y < self.Y_MIN or y > self.Y_MAX:
            self.logger.warning(
                f"Y position {y} is out of bounds [{self.Y_MIN}, {self.Y_MAX}]. Cancelling."
            )
            return f"Y position {y} out of bounds [{self.Y_MIN}, {self.Y_MAX}]", SkillResult.CANCELLED
        
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        self.logger.info(
            f"Moving arm on plane to X={x}, Y={y} "
            f"(Z={self.FIXED_Z}, roll={self.FIXED_ROLL}, pitch={self.FIXED_PITCH}, yaw={self.FIXED_YAW}) "
            f"over {duration}s"
        )
        
        success = self.manipulation.move_to_cartesian_pose(
            x=x,
            y=y,
            z=self.FIXED_Z,
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
        
        return f"Arm moved to X={x}, Y={y}", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
