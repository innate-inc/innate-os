#!/usr/bin/env python3
"""
Torque On Skill - Enable torque on arm motors.
"""

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class TorqueOn(Skill):
    """Enable torque on arm motors to lock position."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    def __init__(self, logger):
        super().__init__(logger)
    
    @property
    def name(self):
        return "torque_on"
    
    def guidelines(self):
        return (
            "Enable torque on all arm motors. Use this after user has manually "
            "positioned the arm to lock it in place."
        )
    
    def execute(self):
        """Enable torque on arm motors."""
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        success = self.manipulation.torque_on()
        if success:
            self._send_feedback("Torque enabled - arm locked in position")
            return "Torque enabled", SkillResult.SUCCESS
        else:
            return "Failed to enable torque", SkillResult.FAILURE
    
    def cancel(self):
        """Nothing to cancel."""
        return "Torque on cannot be cancelled"
