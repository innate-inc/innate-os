#!/usr/bin/env python3
"""
Example primitive demonstrating how to use the ManipulationInterface
to control the arm using inverse kinematics.
"""

from brain_client.primitive_types import Primitive, PrimitiveResult


class MoveArmToPose(Primitive):
    """
    Primitive that moves the arm to a specified Cartesian pose using IK.
    
    This demonstrates how primitives can use the manipulation interface
    to control the arm without needing to know ROS details.
    """
    
    @property
    def name(self):
        return "move_arm_to_pose"
    
    def guidelines(self):
        return """Move the robot's arm to a specific position and orientation in 3D space.
        
Parameters:
- x, y, z: Target position in meters relative to base_link
- roll, pitch, yaw: Target orientation in radians (optional, defaults to 0)
- duration: Time in seconds for the motion (optional, default 3)

Example usage:
- Move arm to position (0.3, 0.1, 0.2) with default orientation over 3 seconds
- Move arm to position (0.25, 0.0, 0.15) with pitch of 1.57 radians over 5 seconds
"""
    
    def guidelines_when_running(self):
        return "The arm is moving to the target pose. Please wait for completion."
    
    def execute(self, x: float, y: float, z: float, 
                roll: float = 0.0, pitch: float = 0.0, yaw: float = 0.0,
                duration: int = 3):
        """
        Execute the arm motion to the target Cartesian pose.
        
        Args:
            x, y, z: Target position in meters
            roll, pitch, yaw: Target orientation in radians
            duration: Motion duration in seconds
            
        Returns:
            Tuple of (message, status)
        """
        if self.manipulation is None:
            return "Manipulation interface not available", PrimitiveResult.FAILURE
        
        self._send_feedback(f"Moving arm to position ({x:.3f}, {y:.3f}, {z:.3f})")
        
        # Use the manipulation interface to move the arm
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=z,
            roll=roll, pitch=pitch, yaw=yaw,
            duration=duration
        )
        
        if success:
            self._send_feedback("Arm successfully moved to target pose")
            return f"Arm moved to ({x:.3f}, {y:.3f}, {z:.3f})", PrimitiveResult.SUCCESS
        else:
            return "Failed to move arm to target pose", PrimitiveResult.FAILURE
    
    def cancel(self):
        """Cancel the arm motion (not implemented for this simple example)."""
        return "Arm motion cancellation not implemented"


class GetEndEffectorPose(Primitive):
    """
    Primitive that returns the current end-effector pose.
    """
    
    @property
    def name(self):
        return "get_end_effector_pose"
    
    def guidelines(self):
        return """Get the current position and orientation of the robot's end-effector (gripper).

Returns the current Cartesian pose of the end-effector relative to the base_link.
This is useful for checking where the arm currently is before planning a motion.
"""
    
    def execute(self):
        """
        Get the current end-effector pose.
        
        Returns:
            Tuple of (message with pose info, status)
        """
        if self.manipulation is None:
            return "Manipulation interface not available", PrimitiveResult.FAILURE
        
        pose = self.manipulation.get_current_end_effector_pose()
        
        if pose is None:
            return "No end-effector pose available yet", PrimitiveResult.FAILURE
        
        pos = pose['position']
        message = (f"End-effector at position: x={pos['x']:.3f}, y={pos['y']:.3f}, z={pos['z']:.3f} "
                  f"in frame {pose['frame_id']}")
        
        self.logger.info(message)
        return message, PrimitiveResult.SUCCESS
    
    def cancel(self):
        """Nothing to cancel for a query operation."""
        return "No operation to cancel"


class MoveArmToJointPositions(Primitive):
    """
    Primitive that moves the arm to specified joint angles.
    Useful when you already know the joint configuration you want.
    """
    
    @property
    def name(self):
        return "move_arm_to_joint_positions"
    
    def guidelines(self):
        return """Move the robot's arm to specific joint angles.

Parameters:
- joint_positions: List of 6 joint angles in radians [j1, j2, j3, j4, j5, j6]
- duration: Time in seconds for the motion (optional, default 3)

This is useful when you have a known joint configuration, such as a home position
or a pre-defined pose. For moving to Cartesian positions, use move_arm_to_pose instead.
"""
    
    def execute(self, joint_positions: list, duration: int = 3):
        """
        Execute the arm motion to target joint positions.
        
        Args:
            joint_positions: List of 6 joint angles in radians
            duration: Motion duration in seconds
            
        Returns:
            Tuple of (message, status)
        """
        if self.manipulation is None:
            return "Manipulation interface not available", PrimitiveResult.FAILURE
        
        if not isinstance(joint_positions, list) or len(joint_positions) != 6:
            return f"Expected list of 6 joint positions, got {joint_positions}", PrimitiveResult.FAILURE
        
        self._send_feedback(f"Moving arm to joint positions: {[f'{j:.3f}' for j in joint_positions]}")
        
        success = self.manipulation.move_to_joint_positions(
            joint_positions=joint_positions,
            duration=duration
        )
        
        if success:
            self._send_feedback("Arm successfully moved to target joint positions")
            return "Arm moved to target joint configuration", PrimitiveResult.SUCCESS
        else:
            return "Failed to move arm to target joint positions", PrimitiveResult.FAILURE
    
    def cancel(self):
        """Cancel the arm motion (not implemented for this simple example)."""
        return "Arm motion cancellation not implemented"
