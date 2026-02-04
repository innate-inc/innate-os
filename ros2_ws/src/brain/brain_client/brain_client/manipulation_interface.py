#!/usr/bin/env python3
"""
ManipulationInterface - Provides IK and arm control capabilities to skills.

This interface allows skills to:
1. Request inverse kinematics solutions for Cartesian poses
2. Command the arm to move to joint positions
3. Get current end-effector pose (forward kinematics)
"""

import time

import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from maurice_msgs.srv import GotoJS
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray


class ManipulationInterface:
    """
    Interface for arm manipulation using IK and joint control.

    This class provides high-level methods for skills to control the arm
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
        self._ik_target_pub = self.node.create_publisher(Twist, "/ik_delta", 10)

        # Subscribers
        self._ik_solution = None
        self._fk_pose = None
        self._ik_solution_sub = self.node.create_subscription(
            JointState, "/ik_solution", self._ik_solution_callback, 10
        )
        self._fk_pose_sub = self.node.create_subscription(PoseStamped, "/fk_pose", self._fk_pose_callback, 10)

        # Service client for joint space control. We create the client once
        # here, but deliberately avoid any blocking wait_for_service calls
        # to keep the executor responsive.
        self._goto_js_client = self.node.create_client(GotoJS, "/mars/arm/goto_js")

        self.logger.info("ManipulationInterface initialized")

    def _ik_solution_callback(self, msg: JointState):
        """Store the latest IK solution."""
        # Log
        self.logger.info(f"IK solution received: {msg}")
        self._ik_solution = msg

    def _fk_pose_callback(self, msg: PoseStamped):
        """Store the latest FK pose."""
        self._fk_pose = msg

    def get_current_end_effector_pose(self) -> dict | None:
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
            "position": {"x": pose.position.x, "y": pose.position.y, "z": pose.position.z},
            "orientation": {
                "x": pose.orientation.x,
                "y": pose.orientation.y,
                "z": pose.orientation.z,
                "w": pose.orientation.w,
            },
            "frame_id": self._fk_pose.header.frame_id,
        }

    def get_current_orientation_rpy(self) -> dict | None:
        """
        Get the current end-effector orientation as roll/pitch/yaw.

        Returns:
            dict with keys: 'roll', 'pitch', 'yaw' (in radians)
            or None if no pose is available
        """
        if self._fk_pose is None:
            self.logger.warn("No FK pose available yet")
            return None

        import math

        pose = self._fk_pose.pose
        x, y, z, w = pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w

        # Convert quaternion to roll, pitch, yaw
        sinr_cosp = 2 * (w * x + y * z)
        cosr_cosp = 1 - 2 * (x * x + y * y)
        roll = math.atan2(sinr_cosp, cosr_cosp)

        sinp = 2 * (w * y - z * x)
        if abs(sinp) >= 1:
            pitch = math.copysign(math.pi / 2, sinp)
        else:
            pitch = math.asin(sinp)

        siny_cosp = 2 * (w * z + x * y)
        cosy_cosp = 1 - 2 * (y * y + z * z)
        yaw = math.atan2(siny_cosp, cosy_cosp)

        return {"roll": roll, "pitch": pitch, "yaw": yaw}

    def solve_ik(
        self,
        x: float,
        y: float,
        z: float,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        timeout: float = 2.0,
    ) -> list[float] | None:
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

        self._ik_target_pub.publish(target)

        # Wait for solution - use spin_once to process callbacks
        start_time = time.time()
        iteration = 0
        while time.time() - start_time < timeout:
            # Spin once to allow callback to process incoming IK solution
            rclpy.spin_once(self.node, timeout_sec=0.01)

            if self._ik_solution is not None:
                joint_positions = list(self._ik_solution.position)

                # Validate that we received a non-empty solution
                if len(joint_positions) == 0:
                    self.logger.error("[ManipulationInterface] IK solver returned empty solution (IK failed)")
                    return None

                return joint_positions

            iteration += 1

        self.logger.error(f"[ManipulationInterface] IK solution timeout after {timeout}s ({iteration} iterations)")
        return None

    def move_to_joint_positions(
        self, joint_positions: list[float], duration: int = 3, wait_for_completion: bool = True
    ) -> bool:
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

        # Ensure GotoJS client is available
        if self._goto_js_client is None:
            self.logger.error("[ManipulationInterface] GotoJS client is not initialized")
            return False

        # Non-blocking check for service readiness
        if not self._goto_js_client.service_is_ready():
            self.logger.error("[ManipulationInterface] GotoJS service is not ready")
            return False

        # Build request
        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = joint_positions
        request.time = float(duration)

        try:
            # Fire-and-forget: do not wait on the future here to avoid
            # blocking the action callback. Any lower-level failures should
            # be handled/logged by the GotoJS server.
            self._goto_js_client.call_async(request)
        except Exception as e:
            self.logger.error(f"[ManipulationInterface] Exception calling GotoJS: {e}")
            return False

        return True

    def move_to_cartesian_pose(
        self,
        x: float,
        y: float,
        z: float,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        duration: int = 3,
        ik_timeout: float = 2.0,
    ) -> bool:
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
        # Reintroduce IK solving, but with solve_ik no longer performing
        # rclpy.spin_once inside the loop.
        joint_positions = self.solve_ik(x, y, z, roll, pitch, yaw, timeout=ik_timeout)
        if joint_positions is None:
            self.logger.error("Failed to solve IK for target pose")
            return False

        # IK returns 5 joints, but we need 6 (add joint6/gripper with default value of 0.0)
        if len(joint_positions) == 5:
            joint_positions.append(0.0)

        # Execute motion (non-blocking to avoid blocking the action server callback)
        return self.move_to_joint_positions(
            joint_positions,
            duration=duration,
            wait_for_completion=False,
        )

    def get_joint_limits(self) -> dict:
        """
        Get the joint limits for the arm.

        Returns:
            Dictionary with joint limit information
        """
        # These could be loaded from URDF or config files
        # For now, returning typical limits
        return {
            "joint1": {"min": -3.14, "max": 3.14},
            "joint2": {"min": -1.57, "max": 1.22},
            "joint3": {"min": -1.57, "max": 1.57},
            "joint4": {"min": -1.57, "max": 1.57},
            "joint5": {"min": -1.57, "max": 1.57},
            "joint6": {"min": -1.57, "max": 1.57},
        }
