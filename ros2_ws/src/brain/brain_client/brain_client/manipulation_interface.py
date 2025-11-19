#!/usr/bin/env python3
"""
ManipulationInterface - Provides IK and arm control capabilities to primitives.

This interface allows primitives to:
1. Request inverse kinematics solutions for Cartesian poses
2. Command the arm to move to joint positions
3. Get current end-effector pose (forward kinematics)
"""

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, PoseStamped
from sensor_msgs.msg import JointState
from maurice_msgs.srv import GotoJS
from std_msgs.msg import Float64MultiArray
import time
from typing import Optional, Tuple, List


class ManipulationInterface:
    """
    Interface for arm manipulation using IK and joint control.
    
    This class provides high-level methods for primitives to control the arm
    without needing to know the details of ROS topics and services.
    """
    
    def __init__(self, node: Node, logger):
        """
        Initialize the manipulation interface.
        
        Args:
            node: ROS2 node for creating publishers/subscribers/clients
            logger: Logger for status messages
        """
        self.node = node
        self.logger = logger
        
        # Publishers
        self._ik_target_pub = self.node.create_publisher(Twist, 'ik_delta', 10)
        
        # Subscribers
        self._ik_solution = None
        self._fk_pose = None
        self._ik_solution_sub = self.node.create_subscription(
            JointState, 'ik_solution', self._ik_solution_callback, 10
        )
        self._fk_pose_sub = self.node.create_subscription(
            PoseStamped, 'fk_pose', self._fk_pose_callback, 10
        )
        
        # Service client for joint space control (lazy initialization)
        self._goto_js_client = None
        
        self.logger.info("ManipulationInterface initialized")
    
    def _ik_solution_callback(self, msg: JointState):
        """Store the latest IK solution."""
        self._ik_solution = msg
    
    def _fk_pose_callback(self, msg: PoseStamped):
        """Store the latest FK pose."""
        self._fk_pose = msg
    
    def get_current_end_effector_pose(self) -> Optional[dict]:
        """
        Get the current end-effector pose in Cartesian space.
        
        Returns:
            dict with keys: 'position' (x, y, z), 'orientation' (x, y, z, w)
            or None if no pose is available
        """
        if self._fk_pose is None:
            self.logger.warn("No FK pose available yet")
            return None
        
        pose = self._fk_pose.pose
        return {
            'position': {
                'x': pose.position.x,
                'y': pose.position.y,
                'z': pose.position.z
            },
            'orientation': {
                'x': pose.orientation.x,
                'y': pose.orientation.y,
                'z': pose.orientation.z,
                'w': pose.orientation.w
            },
            'frame_id': self._fk_pose.header.frame_id
        }
    
    def solve_ik(self, x: float, y: float, z: float, 
                 roll: float = 0.0, pitch: float = 0.0, yaw: float = 0.0,
                 timeout: float = 2.0) -> Optional[List[float]]:
        """
        Solve inverse kinematics for a target Cartesian pose.
        
        Args:
            x, y, z: Target position in meters (relative to base_link)
            roll, pitch, yaw: Target orientation in radians
            timeout: Maximum time to wait for IK solution in seconds
            
        Returns:
            List of joint angles in radians, or None if IK fails
        """
        # Clear previous solution
        self._ik_solution = None
        
        # Publish target pose to IK node
        target = Twist()
        target.linear.x = x
        target.linear.y = y
        target.linear.z = z
        target.angular.x = roll
        target.angular.y = pitch
        target.angular.z = yaw
        
        self.logger.info(f"Requesting IK solution for pose: x={x:.3f}, y={y:.3f}, z={z:.3f}, "
                        f"roll={roll:.3f}, pitch={pitch:.3f}, yaw={yaw:.3f}")
        
        self._ik_target_pub.publish(target)
        
        # Wait for solution
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self._ik_solution is not None:
                joint_positions = list(self._ik_solution.position)
                self.logger.info(f"IK solution received: {[f'{j:.3f}' for j in joint_positions]}")
                return joint_positions
            
            # Spin to process callbacks
            rclpy.spin_once(self.node, timeout_sec=0.01)
            time.sleep(0.01)
        
        self.logger.error(f"IK solution timeout after {timeout}s")
        return None
    
    def move_to_joint_positions(self, joint_positions: List[float], 
                               duration: int = 3,
                               wait_for_completion: bool = True) -> bool:
        """
        Move the arm to specified joint positions using smooth trajectory.
        
        Args:
            joint_positions: List of 6 joint angles in radians
            duration: Trajectory duration in seconds
            wait_for_completion: If True, block until motion completes
            
        Returns:
            True if command was successful, False otherwise
        """
        if len(joint_positions) != 6:
            self.logger.error(f"Expected 6 joint positions, got {len(joint_positions)}")
            return False
        
        # Lazy initialization of service client
        if self._goto_js_client is None:
            self.logger.info("Creating GotoJS service client")
            self._goto_js_client = self.node.create_client(GotoJS, '/mars/arm/goto_js')
        
        # Wait for service to be available
        if not self._goto_js_client.wait_for_service(timeout_sec=5.0):
            self.logger.error("GotoJS service not available")
            return False
        
        # Create service request
        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = joint_positions
        request.time = duration
        
        self.logger.info(f"Commanding arm to joint positions: {[f'{j:.3f}' for j in joint_positions]} "
                        f"over {duration}s")
        
        # Call service
        future = self._goto_js_client.call_async(request)
        
        if wait_for_completion:
            # Wait for service response
            rclpy.spin_until_future_complete(self.node, future, timeout_sec=10.0)
            
            if future.done():
                response = future.result()
                if response.success:
                    self.logger.info("Arm motion command accepted")
                    # Wait for motion to complete
                    time.sleep(duration + 0.5)  # Add buffer time
                    return True
                else:
                    self.logger.error("Arm motion command rejected")
                    return False
            else:
                self.logger.error("Service call timeout")
                return False
        else:
            # Non-blocking - just send the command
            return True
    
    def move_to_cartesian_pose(self, x: float, y: float, z: float,
                              roll: float = 0.0, pitch: float = 0.0, yaw: float = 0.0,
                              duration: int = 3,
                              ik_timeout: float = 2.0) -> bool:
        """
        Move the arm to a Cartesian pose (combines IK solving and motion execution).
        
        Args:
            x, y, z: Target position in meters (relative to base_link)
            roll, pitch, yaw: Target orientation in radians
            duration: Trajectory duration in seconds
            ik_timeout: Maximum time to wait for IK solution
            
        Returns:
            True if successful, False otherwise
        """
        # Solve IK
        joint_positions = self.solve_ik(x, y, z, roll, pitch, yaw, timeout=ik_timeout)
        
        if joint_positions is None:
            self.logger.error("Failed to solve IK for target pose")
            return False
        
        # IK returns 5 joints, but we need 6 (add joint6/gripper with default value of 0.0)
        if len(joint_positions) == 5:
            joint_positions.append(0.0)
            self.logger.info("Added joint6 (gripper) with default value 0.0")
        
        # Execute motion
        return self.move_to_joint_positions(joint_positions, duration=duration)
    
    def get_joint_limits(self) -> dict:
        """
        Get the joint limits for the arm.
        
        Returns:
            Dictionary with joint limit information
        """
        # These could be loaded from URDF or config files
        # For now, returning typical limits
        return {
            'joint1': {'min': -3.14, 'max': 3.14},
            'joint2': {'min': -1.57, 'max': 1.22},
            'joint3': {'min': -1.57, 'max': 1.57},
            'joint4': {'min': -1.57, 'max': 1.57},
            'joint5': {'min': -1.57, 'max': 1.57},
            'joint6': {'min': -1.57, 'max': 1.57},
        }
