#!/usr/bin/env python3
"""
Arm Zero Position Skill - Move arm to all-zeros joint position.
"""

import time
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class ArmZeroPosition(Skill):
    """Move the arm to the zero position (all joints at 0 radians)."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "arm_zero_position"
    
    def guidelines(self):
        return "Use this to move the arm to its zero/home position where all joints are at 0 radians."
    
    def execute(self, duration: int = 3):
        """Execute the arm movement to zero position."""
        self._cancelled = False
        
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        self.logger.info(f"Moving arm to zero position [0,0,0,0,0,0] over {duration}s")
        
        joint_positions = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        
        success = self.manipulation.move_to_joint_positions(
            joint_positions=joint_positions,
            duration=duration,
            wait_for_completion=False
        )
        
        if not success:
            return "Failed to send arm command", SkillResult.FAILURE
        
        # Wait for motion to complete (with cancellation check)
        start_time = time.time()
        while time.time() - start_time < duration:
            if self._cancelled:
                return "Arm motion cancelled", SkillResult.CANCELLED
            time.sleep(0.1)
        
        return "Arm moved to zero position", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
