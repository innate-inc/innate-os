#!/usr/bin/env python3
"""
Get Arm Pose Skill - Retrieve current arm end-effector pose via FK.
"""

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


class GetArmPose(Skill):
    """Retrieve the current arm end-effector pose using forward kinematics."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    def __init__(self, logger):
        super().__init__(logger)
    
    @property
    def name(self):
        return "get_arm_pose"
    
    def guidelines(self):
        return (
            "Get the current arm end-effector position and orientation. "
            "Returns the XYZ position in meters and roll/pitch/yaw orientation in radians. "
            "No parameters required."
        )
    
    def execute(self):
        """
        Get the current arm end-effector pose.
        
        Returns:
            Current pose information including position (x, y, z) and orientation (roll, pitch, yaw)
        """
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        # Get current position
        current_pose = self.manipulation.get_current_end_effector_pose()
        if current_pose is None:
            return "Could not get current arm pose - FK not available", SkillResult.FAILURE
        
        # Get current orientation as RPY
        current_rpy = self.manipulation.get_current_orientation_rpy()
        
        x = current_pose["position"]["x"]
        y = current_pose["position"]["y"]
        z = current_pose["position"]["z"]
        
        result_msg = f"Current arm pose: X={x:.3f}, Y={y:.3f}, Z={z:.3f}"
        
        if current_rpy is not None:
            roll = current_rpy["roll"]
            pitch = current_rpy["pitch"]
            yaw = current_rpy["yaw"]
            result_msg += f", Roll={roll:.3f}, Pitch={pitch:.3f}, Yaw={yaw:.3f}"
        
        self.logger.info(result_msg)
        
        # Send FK pose as feedback to the agent
        self._send_feedback(result_msg)
        
        return result_msg, SkillResult.SUCCESS
    
    def cancel(self):
        """Nothing to cancel for a read-only operation."""
        return "Get arm pose cannot be cancelled"
