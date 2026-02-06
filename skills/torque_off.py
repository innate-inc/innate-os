#!/usr/bin/env python3
"""
Torque Off Skill - Disable torque on arm motors so user can manually move it.
"""

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class TorqueOff(Skill):
    """Disable torque on arm motors, allowing manual positioning."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    def __init__(self, logger):
        super().__init__(logger)
    
    @property
    def name(self):
        return "torque_off"
    
    def guidelines(self):
        return (
            "Disable torque on all arm motors. The arm will go limp and can be "
            "manually moved by the user. Use this before asking user to position the arm."
        )
    
    def execute(self):
        """Disable torque on arm motors."""
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        success = self.manipulation.torque_off()
        if success:
            self._send_feedback("Torque disabled - arm can be moved manually")
            return "Torque disabled", SkillResult.SUCCESS
        else:
            return "Failed to disable torque", SkillResult.FAILURE
    
    def cancel(self):
        """Nothing to cancel."""
        return "Torque off cannot be cancelled"
