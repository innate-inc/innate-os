#!/usr/bin/env python3
"""
Check Arm Status Skill - Read current arm health and torque state on demand.
"""

import time
from maurice_msgs.msg import ArmStatus
from brain_client.skill_types import Skill, SkillResult


class CheckArmStatus(Skill):
    """Check if arm servos are healthy and torque is enabled."""
    
    def __init__(self, logger):
        super().__init__(logger)
        self._last_msg = None
    
    @property
    def name(self):
        return "check_arm_status"
    
    def guidelines(self):
        return (
            "Check the current arm status: whether servos are healthy and torque is enabled. "
            "Use this before starting arm movements to verify the arm is ready. "
            "If torque is off or servos have errors, call reload_servos to recover."
        )
    
    def _on_msg(self, msg):
        self._last_msg = msg
    
    def execute(self):
        """Read one ArmStatus message and report state."""
        if not self.node:
            return "No ROS node available", SkillResult.FAILURE
        
        self._last_msg = None
        
        sub = self.node.create_subscription(
            ArmStatus, '/mars/arm/status', self._on_msg, 10
        )
        
        # Wait for callback (executor already spins the node)
        for _ in range(50):
            if self._last_msg is not None:
                break
            time.sleep(0.1)
        
        self.node.destroy_subscription(sub)
        
        if self._last_msg is None:
            self._send_feedback("Arm status topic not available. Assuming arm is ready — proceed with caution.")
            return "Arm status unknown (topic not publishing). Proceed with calibration.", SkillResult.SUCCESS
        
        msg = self._last_msg
        is_ok = msg.is_ok
        torque = msg.is_torque_enabled
        error = msg.error
        
        status = f"Servos: {'OK' if is_ok else 'FAULT (' + error + ')'}. Torque: {'ON' if torque else 'OFF'}."
        
        if is_ok and torque:
            self._send_feedback(f"Arm ready. {status}")
            return f"Arm ready. {status}", SkillResult.SUCCESS
        else:
            self._send_feedback(f"Arm NOT ready. {status} Call reload_servos to recover.")
            return f"Arm NOT ready. {status} Call reload_servos to recover.", SkillResult.SUCCESS
    
    def cancel(self):
        return "Nothing to cancel"
