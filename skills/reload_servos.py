#!/usr/bin/env python3
"""
Reload Servos Skill - Reboot arm servos, re-enable torque, and move arm
back to the center of the working plane. Full recovery in one call.
"""

import time
import rclpy
from std_srvs.srv import Trigger
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class ReloadServos(Skill):
    """Reboot servos, enable torque, and reposition arm to center of plane."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    # Center of the working plane
    CENTER_X = 0.25
    CENTER_Y = -0.05
    FIXED_Z = 0.1
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57
    
    def __init__(self, logger):
        super().__init__(logger)
        self._client = None
    
    @property
    def name(self):
        return "reload_servos"
    
    def guidelines(self):
        return (
            "Full arm recovery: reboots all servos, re-enables torque, and moves the arm "
            "back to the center of the working plane (X=0.25, Y=-0.05, Z=0.1). "
            "Use when arm_vitals reports a servo failure. "
            "After this skill completes, the arm is ready to use again."
        )
    
    def execute(self):
        """Reboot servos, enable torque, reposition arm."""
        if not self.node:
            return "No ROS node available", SkillResult.FAILURE
        
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        if self._client is None:
            self._client = self.node.create_client(Trigger, "/mars/arm/reboot")
        
        if not self._client.wait_for_service(timeout_sec=5.0):
            return "/mars/arm/reboot service not available", SkillResult.FAILURE
        
        # Step 1: Reboot servos
        self.logger.info("[ReloadServos] Rebooting servos...")
        self._send_feedback("Rebooting servos...")
        request = Trigger.Request()
        future = self._client.call_async(request)
        
        try:
            rclpy.spin_until_future_complete(self.node, future, timeout_sec=10.0)
        except Exception as e:
            return f"Reboot service call failed: {e}", SkillResult.FAILURE
        
        if not future.done():
            return "Reboot service call timed out", SkillResult.FAILURE
        
        result = future.result()
        if not result.success:
            return f"Servo reboot failed: {result.message}", SkillResult.FAILURE
        
        # Step 2: Enable torque
        self._send_feedback("Enabling torque...")
        time.sleep(1.0)
        success = self.manipulation.torque_on()
        if not success:
            return "Servos rebooted but failed to enable torque", SkillResult.FAILURE
        
        # Step 3: Move arm to center of plane
        self._send_feedback("Moving arm to center of plane...")
        time.sleep(0.5)
        success = self.manipulation.move_to_cartesian_pose(
            x=self.CENTER_X, y=self.CENTER_Y, z=self.FIXED_Z,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=2
        )
        if not success:
            return "Torque enabled but failed to move arm to center", SkillResult.FAILURE
        
        time.sleep(2)
        self._send_feedback(f"Recovery complete. Arm at X={self.CENTER_X}, Y={self.CENTER_Y}, Z={self.FIXED_Z}")
        return f"Arm recovered and positioned at center (X={self.CENTER_X}, Y={self.CENTER_Y})", SkillResult.SUCCESS
    
    def cancel(self):
        """Nothing to cancel."""
        return "Reload cannot be cancelled"
