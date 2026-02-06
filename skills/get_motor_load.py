#!/usr/bin/env python3
"""
Get Motor Load Skill - Retrieve current motor load/effort from arm actuators.
"""

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class GetMotorLoad(Skill):
    """Retrieve the current motor load/effort for all arm joints."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    def __init__(self, logger):
        super().__init__(logger)
    
    @property
    def name(self):
        return "get_motor_load"
    
    def guidelines(self):
        return (
            "Get the current motor load/effort for all arm joints. "
            "Returns load as percentage (-100% to 100%) for each of the 6 arm joints. "
            "Higher absolute values indicate more force/resistance on the motors. "
            "Useful for detecting contact or obstacles. No parameters required."
        )
    
    def execute(self):
        """
        Get the current motor load/effort.
        
        Returns:
            Current load values for all 6 arm joints as percentages
        """
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        motor_load = self.manipulation.get_motor_load()
        if motor_load is None:
            return "Could not get motor load - arm state not available", SkillResult.FAILURE
        
        # Format the result message
        load_strs = [f"J{i+1}={load:.1f}%" for i, load in enumerate(motor_load)]
        result_msg = f"Motor load: {', '.join(load_strs)}"
        
        self.logger.info(result_msg)
        
        # Send as feedback to the agent
        self._send_feedback(result_msg)
        
        return result_msg, SkillResult.SUCCESS
    
    def cancel(self):
        """Nothing to cancel for a read-only operation."""
        return "Get motor load cannot be cancelled"
