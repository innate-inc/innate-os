#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
KDL-based IK node loading URDF directly from mars_description.
"""

import math
import os
import time

import numpy as np
import PyKDL as kdl
import rclpy
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseStamped, Twist
from rclpy.node import Node
from sensor_msgs.msg import JointState
from urdf_parser_py.urdf import URDF

from mars_arm.urdf import treeFromUrdfModel


def _request_key(t: Twist) -> str:
    """The request pose, echoed in every reply's frame_id: two clients share
    /ik_delta and /ik_solution with no other correlation, and each must be
    able to tell its own answer from the other's. Manipulation._solve_ik
    builds the same string from the floats it sent."""
    return f"{t.linear.x:.4f} {t.linear.y:.4f} {t.linear.z:.4f} {t.angular.x:.4f} {t.angular.y:.4f} {t.angular.z:.4f}"


def _target_frame(t: Twist) -> kdl.Frame:
    return kdl.Frame(
        kdl.Rotation.RPY(t.angular.x, t.angular.y, t.angular.z), kdl.Vector(t.linear.x, t.linear.y, t.linear.z)
    )


def _jnt_array(q: np.ndarray) -> kdl.JntArray:
    jnt = kdl.JntArray(len(q))
    for i, angle in enumerate(q):
        jnt[i] = float(angle)
    return jnt


class KDLIKNode(Node):
    # Cartesian error (m + 0.1 * rad) under which a seed's solution is taken
    # as-is instead of being outvoted by another seed's marginally better fit.
    CONTINUITY_SCORE = 0.005
    # Radians past a URDF limit still accepted; the servo clamps the rest, a few mm at the
    # gripper. drop_in_box's release at pitch 1.30 needs joint4 up to ~0.015 past its stop.
    LIMIT_SLACK = 0.03
    # LMA's default weights: a radian of orientation miss counts as a centimetre of position.
    POSE_WEIGHTS = np.array([1.0, 1.0, 1.0, 0.01, 0.01, 0.01])
    STREAM_MAX_ITER = 100

    def __init__(self):
        super().__init__("kdl_ik_from_file")

        # 1) Declare & read solver parameters
        self.declare_parameter("search_resolution", 0.0001)
        self.declare_parameter("timeout", 0.2)
        eps = self.get_parameter("search_resolution").value
        timeout = self.get_parameter("timeout").value
        maxiter = max(1, int(timeout / eps))

        # 2) Load URDF file directly from mars_description package
        pkg_dir = get_package_share_directory("mars_description")
        urdf_path = os.path.join(pkg_dir, "urdf", "mars.urdf")
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
        self.jac_solver = kdl.ChainJntToJacSolver(self.chain)

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

        # LMA ignores joint limits, and the driver clamps an overrun silently:
        # the arm would land somewhere nobody asked for.
        self.joint_limits = [
            (robot_model.joint_map[name].limit.lower, robot_model.joint_map[name].limit.upper)
            for name in self.joint_names
        ]
        self.lower = np.array([lower for lower, _ in self.joint_limits])
        self.upper = np.array([upper for _, upper in self.joint_limits])

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
        self.fk_pub = self.create_publisher(PoseStamped, "/fk_pose", 10)
        self.create_subscription(Twist, "/ik_delta", self.on_delta, 10)
        self.create_subscription(Twist, "/ik_stream", self.on_stream, 10)
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
        if self.latest_joint_states is None or self.fk_pub.get_subscription_count() == 0:
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

    def _within_limits(self, q: kdl.JntArray) -> bool:
        return all(
            lower - self.LIMIT_SLACK <= self._normalize_angle(q[i]) <= upper + self.LIMIT_SLACK
            for i, (lower, upper) in enumerate(self.joint_limits)
        )

    def _normalize_angle(self, angle):
        """Normalize angle to [-pi, pi]."""
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    def on_delta(self, delta: Twist):
        """Handle absolute target pose (sent via Twist message for compatibility)"""
        key = _request_key(delta)
        target_frame = _target_frame(delta)

        # Log the target frame before IK
        target_pos = target_frame.p
        target_rot = target_frame.M.GetRPY()
        self.get_logger().debug(
            f"IK Target (absolute w.r.t. base) - Pos (x,y,z): ({target_pos.x():.4f}, {target_pos.y():.4f}, {target_pos.z():.4f}), "
            f"RPY: ({target_rot[0]:.4f}, {target_rot[1]:.4f}, {target_rot[2]:.4f})"
        )

        start_time = time.perf_counter()

        # The zero seed only votes when the current posture lands short, or a
        # streamed target hops between elbow branches.
        seeds = [
            ("current", self.current_q),
            ("zeros", kdl.JntArray(self.chain.getNrOfJoints())),  # initialized to zeros
        ]

        best_solution = None
        best_score = float("inf")
        best_seed_name = None

        for seed_name, seed in seeds:
            success, q_out, score = self._try_ik_with_seed(seed, target_frame)
            if success and not self._within_limits(q_out):
                self.get_logger().debug(f"IK ({seed_name} seed) solution violates joint limits — rejected")
                success = False
            if success and score < best_score:
                best_solution = q_out
                best_score = score
                best_seed_name = seed_name
            if best_score < self.CONTINUITY_SCORE:
                break

        solve_time_ms = (time.perf_counter() - start_time) * 1000

        if best_solution is None:
            self.get_logger().warning(
                f"KDL IK found no solution within joint limits (took {solve_time_ms:.2f} ms)",
                throttle_duration_sec=1.0,
            )
            # An empty solution is the answer "unreachable": without it the
            # asker learns nothing until its timeout.
            reply = JointState()
            reply.header.frame_id = key
            self.joint_pub.publish(reply)
            return

        self.get_logger().debug(
            f"KDL IK solved (seed={best_seed_name}, score={best_score:.4f}, took {solve_time_ms:.2f} ms)"
        )

        # publish JointState
        js = JointState()
        js.header.stamp = self.get_clock().now().to_msg()
        js.header.frame_id = key
        js.name = self.joint_names
        js.position = [self._normalize_angle(best_solution[i]) for i in range(best_solution.rows())]
        self.joint_pub.publish(js)

    def on_stream(self, request: Twist) -> None:
        """Best-effort IK for a streamed target: the in-limits posture nearest
        it, from the current one, with the position miss (m) in effort[0].
        A streamed hand must slide along the edge of reach, not freeze at the
        last target inside it, so this never answers "unreachable"."""
        target = _target_frame(request)
        q = self._nearest_in_limits(target)
        js = JointState()
        js.header.stamp = self.get_clock().now().to_msg()
        js.header.frame_id = f"stream {_request_key(request)}"  # never mistaken for a strict answer
        js.name = self.joint_names
        js.position = q.tolist()
        js.effort = [(target.p - self._fk(q).p).Norm()]  # JointState has no field for the miss
        self.joint_pub.publish(js)

    def _nearest_in_limits(self, target: kdl.Frame) -> np.ndarray:
        """Damped least squares over the joint-limit box: a joint pinned at a
        limit drops out of the step, so the others re-solve around it instead
        of compensating for an angle it cannot take."""
        q = np.clip([self.current_q[i] for i in range(self.current_q.rows())], self.lower, self.upper)
        error = self._weighted_error(q, target)
        damping = 1e-3
        for _ in range(self.STREAM_MAX_ITER):
            jac = self.POSE_WEIGHTS[:, None] * self._jacobian(q)
            gradient = jac.T @ error
            free = ~(((q <= self.lower) & (gradient < 0)) | ((q >= self.upper) & (gradient > 0)))
            if not free.any():
                break
            jf = jac[:, free]
            step = np.zeros_like(q)
            step[free] = np.linalg.solve(jf.T @ jf + damping * np.eye(jf.shape[1]), jf.T @ error)
            candidate = np.clip(q + step, self.lower, self.upper)
            candidate_error = self._weighted_error(candidate, target)
            gain = error @ error - candidate_error @ candidate_error
            if gain > 0:
                q, error, damping = candidate, candidate_error, damping / 3
                if gain < 1e-12:
                    break
            else:
                damping *= 4
                if damping > 1e3:
                    break
        return q

    def _fk(self, q: np.ndarray) -> kdl.Frame:
        frame = kdl.Frame()
        self.fksolver.JntToCart(_jnt_array(q), frame)
        return frame

    def _jacobian(self, q: np.ndarray) -> np.ndarray:
        jac = kdl.Jacobian(len(q))
        self.jac_solver.JntToJac(_jnt_array(q), jac)
        return np.array([[jac[row, col] for col in range(len(q))] for row in range(6)])

    def _weighted_error(self, q: np.ndarray, target: kdl.Frame) -> np.ndarray:
        twist = kdl.diff(self._fk(q), target)
        return self.POSE_WEIGHTS * np.array([twist.vel[i] for i in range(3)] + [twist.rot[i] for i in range(3)])


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
