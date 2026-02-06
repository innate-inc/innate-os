#!/usr/bin/env python3
"""
Record Position Skill - Record current arm FK position and send as feedback.
"""

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class RecordPosition(Skill):
    """Record current arm position and send coordinates as feedback."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    def __init__(self, logger):
        super().__init__(logger)
    
    @property
    def name(self):
        return "record_position"
    
    def guidelines(self):
        return (
            "Record the current arm end-effector position (FK) and report coordinates. "
            "User triggers this vocally when arm is in desired position. "
            "Returns X, Y, Z coordinates for you to remember."
        )
    
    def execute(self):
        """Record and report current FK position."""
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        fk_pose = self.manipulation.get_current_end_effector_pose()
        
        if fk_pose:
            pos = fk_pose["position"]
            position_str = f"X={pos['x']:.4f}, Y={pos['y']:.4f}, Z={pos['z']:.4f}"
            self._send_feedback(f"RECORDED POSITION: {position_str}")
            return f"Position recorded: {position_str}", SkillResult.SUCCESS
        else:
            return "Could not get current position", SkillResult.FAILURE
    
    def cancel(self):
        """Nothing to cancel."""
        return "Record position cannot be cancelled"
