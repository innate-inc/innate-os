#!/usr/bin/env python3
"""
KDL-based IK node loading URDF directly from maurice_sim and using the package-local URDF→KDL parser (urdf.py).
"""

import math
import os
import time

import PyKDL as kdl
import rclpy
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseStamped, Twist
from rclpy.node import Node
from sensor_msgs.msg import JointState
from urdf_parser_py.urdf import URDF

from maurice_arm.urdf import treeFromUrdfModel  # local parser in urdf.py


class KDLIKNode(Node):
    def __init__(self):
        super().__init__("kdl_ik_from_file")

        # 1) Declare & read solver parameters
        self.declare_parameter("search_resolution", 0.0001)
        self.declare_parameter("timeout", 0.2)
        eps = self.get_parameter("search_resolution").value
        timeout = self.get_parameter("timeout").value
        maxiter = max(1, int(timeout / eps))

        # 2) Load URDF file directly from maurice_sim package
        pkg_dir = get_package_share_directory("maurice_sim")
        urdf_path = os.path.join(pkg_dir, "urdf", "maurice.urdf")
        if not os.path.exists(urdf_path):
            self.get_logger().fatal(f"URDF file not found: {urdf_path}")
            raise FileNotFoundError(urdf_path)

        # parse model
        robot_model = URDF.from_xml_file(urdf_path)

        # 3) build KDL tree and chain using local parser
        ok, tree = treeFromUrdfModel(robot_model)
        if not ok or tree is None:
            self.get_logger().fatal("Failed to build KDL tree from URDF")
            raise RuntimeError("URDF→KDL parse error")

        base_link = "base_link"
        tip_link = "ee_link"
        self.chain = tree.getChain(base_link, tip_link)

        # 4) FK and IK solver setup
        self.fksolver = kdl.ChainFkSolverPos_recursive(self.chain)
        self.ik_solver = kdl.ChainIkSolverPos_LMA(self.chain, eps=eps, maxiter=maxiter)

        # 5) prepare joint array and names
        nj = self.chain.getNrOfJoints()
        self.current_q = kdl.JntArray(nj)  # Initialized to zeros

        # Get joint names directly from the KDL chain segments
        self.joint_names = []
        for i in range(self.chain.getNrOfSegments()):
            segment = self.chain.getSegment(i)
            joint = segment.getJoint()

            # Only include non-fixed joints
            if joint.getType() != kdl.Joint.Fixed:
                self.joint_names.append(joint.getName())

        # Verify number of joints matches
        if nj != len(self.joint_names):
            self.get_logger().warn(
                f"KDL chain reports {nj} joints, but found {len(self.joint_names)} names: {self.joint_names}"
            )

        self.get_logger().info(f"IK using joints: {self.joint_names}")

        # Calculate and store initial FK pose (corresponding to q=0)
        self.initial_frame = kdl.Frame()
        fk_result = self.fksolver.JntToCart(self.current_q, self.initial_frame)
        if fk_result >= 0:
            pos = self.initial_frame.p
            rot = self.initial_frame.M.GetRPY()
            self.get_logger().info("Initial FK pose (ee_link relative to base_link):")  # Updated message
            self.get_logger().info(f"  Position (x,y,z): ({pos.x():.4f}, {pos.y():.4f}, {pos.z():.4f})")
            self.get_logger().info(f"  Orientation (r,p,y): ({rot[0]:.4f}, {rot[1]:.4f}, {rot[2]:.4f})")
        else:
            self.get_logger().warn(f"Initial FK calculation failed with code: {fk_result}")
            # Handle error appropriately, maybe raise exception or set a flag
            self.initial_frame = None

        # 6) publisher and subscription
        self.joint_pub = self.create_publisher(JointState, "/ik_solution", 10)
        self.ik_fk_pub = self.create_publisher(PoseStamped, "/ik_solution_fk", 10)
        self.fk_pub = self.create_publisher(PoseStamped, "/fk_pose", 10)
        # self.command_pub = self.create_publisher(Float64MultiArray, '/maurice_arm/commands', 10)
        self.create_subscription(Twist, "/ik_delta", self.on_delta, 10)
        self.create_subscription(JointState, "/mars/arm/state", self.on_joint_states, 10)

        # Timer for FK publishing at 10Hz
        self.create_timer(0.1, self.publish_fk)  # 0.1 seconds = 10Hz

        # Store latest joint states
        self.latest_joint_states = None

        self.get_logger().info(f"KDL IK node ready (eps={eps}, maxiter={maxiter})")

    def on_joint_states(self, msg: JointState):
        """Store the latest joint states and update IK seed"""
        self.latest_joint_states = msg

        # Update current_q (IK seed) from actual joint positions
        for i, joint_name in enumerate(self.joint_names):
            if joint_name in msg.name:
                joint_idx = msg.name.index(joint_name)
                self.current_q[i] = msg.position[joint_idx]

    def publish_fk(self):
        """Timer callback to publish FK result at 10Hz"""
        if self.latest_joint_states is None:
            return

        # Create joint array from received joint states
        q = kdl.JntArray(self.chain.getNrOfJoints())

        # Map joint states to our joint names
        for i, joint_name in enumerate(self.joint_names):
            if joint_name in self.latest_joint_states.name:
                joint_idx = self.latest_joint_states.name.index(joint_name)
                q[i] = self.latest_joint_states.position[joint_idx]

        # Compute FK
        fk_frame = kdl.Frame()
        fk_result = self.fksolver.JntToCart(q, fk_frame)

        if fk_result >= 0:
            # Create PoseStamped message
            pose_msg = PoseStamped()
            pose_msg.header.stamp = self.get_clock().now().to_msg()
            pose_msg.header.frame_id = "base_link"

            # Position (x, y, z)
            pose_msg.pose.position.x = fk_frame.p.x()
            pose_msg.pose.position.y = fk_frame.p.y()
            pose_msg.pose.position.z = fk_frame.p.z()

            # Orientation (quaternion)
            quat = fk_frame.M.GetQuaternion()
            pose_msg.pose.orientation.x = quat[0]
            pose_msg.pose.orientation.y = quat[1]
            pose_msg.pose.orientation.z = quat[2]
            pose_msg.pose.orientation.w = quat[3]

            self.fk_pub.publish(pose_msg)

    def _try_ik_with_seed(self, seed: kdl.JntArray, target_frame: kdl.Frame):
        """Try IK from a given seed. Returns (success, q_out, score) or (False, None, inf).
        Score is the Cartesian error (position + orientation distance from target).
        """
        q_out = kdl.JntArray(self.chain.getNrOfJoints())
        ik_result = self.ik_solver.CartToJnt(seed, target_frame, q_out)

        # Accept successful results and "close enough" warnings
        if ik_result >= 0 or ik_result in (-100, -101):
            # Compute FK on solution to measure actual Cartesian error
            fk_frame = kdl.Frame()
            self.fksolver.JntToCart(q_out, fk_frame)

            # Position error (Euclidean distance)
            pos_err = (target_frame.p - fk_frame.p).Norm()

            # Orientation error (angle between rotations)
            rot_diff = target_frame.M.Inverse() * fk_frame.M
            angle_err = rot_diff.GetRotAngle()[0]  # returns (angle, axis)

            # Combined score (weight orientation error, since it's in radians)
            score = pos_err + 0.1 * abs(angle_err)
            return True, q_out, score
        return False, None, float("inf")

    def _normalize_angle(self, angle):
        """Normalize angle to [-pi, pi]."""
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    def on_delta(self, delta: Twist):
        """Handle absolute target pose (sent via Twist message for compatibility)"""
        # Create target frame from absolute pose values
        target_frame = kdl.Frame()

        # Set position directly (absolute with respect to base)
        target_frame.p.x(delta.linear.x)
        target_frame.p.y(delta.linear.y)
        target_frame.p.z(delta.linear.z)

        # Set orientation from RPY values
        target_frame.M = kdl.Rotation.RPY(delta.angular.x, delta.angular.y, delta.angular.z)

        # Log the target frame before IK
        target_pos = target_frame.p
        target_rot = target_frame.M.GetRPY()
        self.get_logger().info(
            f"IK Target (absolute w.r.t. base) - Pos (x,y,z): ({target_pos.x():.4f}, {target_pos.y():.4f}, {target_pos.z():.4f}), "
            f"RPY: ({target_rot[0]:.4f}, {target_rot[1]:.4f}, {target_rot[2]:.4f})"
        )

        # Multi-start IK: try from current position and from zeros, pick best
        start_time = time.perf_counter()

        seeds = [
            ("current", self.current_q),
            ("zeros", kdl.JntArray(self.chain.getNrOfJoints())),  # initialized to zeros
        ]

        best_solution = None
        best_score = float("inf")
        best_seed_name = None

        for seed_name, seed in seeds:
            success, q_out, score = self._try_ik_with_seed(seed, target_frame)
            if success and score < best_score:
                best_solution = q_out
                best_score = score
                best_seed_name = seed_name

        solve_time_ms = (time.perf_counter() - start_time) * 1000

        if best_solution is None:
            self.get_logger().error(f"KDL IK failed from all seeds (took {solve_time_ms:.2f} ms)")
            return

        self.get_logger().info(
            f"KDL IK solved (seed={best_seed_name}, score={best_score:.4f}, took {solve_time_ms:.2f} ms)"
        )

        # publish JointState
        js = JointState()
        js.header.stamp = self.get_clock().now().to_msg()
        js.name = self.joint_names
        js.position = [self._normalize_angle(best_solution[i]) for i in range(best_solution.rows())]
        self.joint_pub.publish(js)

        # publish FK of the IK solution (what the commanded joints map to)
        fk_frame = kdl.Frame()
        if self.fksolver.JntToCart(best_solution, fk_frame) >= 0:
            ik_fk_msg = PoseStamped()
            ik_fk_msg.header.stamp = js.header.stamp
            ik_fk_msg.header.frame_id = "base_link"
            ik_fk_msg.pose.position.x = fk_frame.p.x()
            ik_fk_msg.pose.position.y = fk_frame.p.y()
            ik_fk_msg.pose.position.z = fk_frame.p.z()
            quat = fk_frame.M.GetQuaternion()
            ik_fk_msg.pose.orientation.x = quat[0]
            ik_fk_msg.pose.orientation.y = quat[1]
            ik_fk_msg.pose.orientation.z = quat[2]
            ik_fk_msg.pose.orientation.w = quat[3]
            self.ik_fk_pub.publish(ik_fk_msg)

        # Publish command for the arm - COMMENTED OUT
        # cmd_msg = Float64MultiArray()
        # # Get the 5 joint values from IK solution
        # ik_positions = [q_out[i] for i in range(q_out.rows())]
        # # Append 0.0 for the 6th joint (joint6)
        # ik_positions.append(0.0)
        # cmd_msg.data = ik_positions
        # self.command_pub.publish(cmd_msg)

        # seed next solve with the result
        self.current_q = q_out


def main(args=None):
    rclpy.init(args=args)
    node = KDLIKNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
