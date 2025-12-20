#!/usr/bin/env python3
"""
KDL-based IK node loading URDF directly from maurice_sim and using the package-local URDF→KDL parser (urdf.py).
"""
import os
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, PoseStamped
from sensor_msgs.msg import JointState
from urdf_parser_py.urdf import URDF
from maurice_arm.urdf import treeFromUrdfModel  # local parser in urdf.py
import PyKDL as kdl
from ament_index_python.packages import get_package_share_directory
import time

class KDLIKNode(Node):
    def __init__(self):
        super().__init__('kdl_ik_from_file')
        # 1) declare & read solver parameters
        self.declare_parameter('search_resolution', 0.001)
        self.declare_parameter('timeout', 0.2)
        eps     = self.get_parameter('search_resolution').value
        timeout = self.get_parameter('timeout').value
        maxiter = max(1, int(timeout / eps))

        # 2) load URDF file directly from maurice_sim package
        pkg_dir = get_package_share_directory('maurice_sim')
        urdf_path = os.path.join(pkg_dir, 'urdf', 'maurice.urdf')
        if not os.path.exists(urdf_path):
            self.get_logger().fatal(f"URDF file not found: {urdf_path}")
            raise FileNotFoundError(urdf_path)

        # parse model
        robot_model = URDF.from_xml_file(urdf_path)

        # 3) build KDL tree and chain using local parser
        ok, tree = treeFromUrdfModel(robot_model)
        if not ok or tree is None:
            self.get_logger().fatal('Failed to build KDL tree from URDF')
            raise RuntimeError('URDF→KDL parse error')

        base_link = 'base_link'
        tip_link  = 'ee_link'
        self.chain = tree.getChain(base_link, tip_link)

        # 4) FK and IK solver setup
        self.fksolver  = kdl.ChainFkSolverPos_recursive(self.chain)
        self.ik_solver = kdl.ChainIkSolverPos_LMA(self.chain, eps=eps, maxiter=maxiter)

        # 5) prepare joint array and names
        nj = self.chain.getNrOfJoints()
        self.current_q = kdl.JntArray(nj) # Initialized to zeros

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
             self.get_logger().warn(f"KDL chain reports {nj} joints, but found {len(self.joint_names)} names: {self.joint_names}")

        self.get_logger().info(f"IK using joints: {self.joint_names}")

        # Calculate and store initial FK pose (corresponding to q=0)
        self.initial_frame = kdl.Frame()
        fk_result = self.fksolver.JntToCart(self.current_q, self.initial_frame)
        if fk_result >= 0:
            pos = self.initial_frame.p
            rot = self.initial_frame.M.GetRPY()
            self.get_logger().info(f"Initial FK pose (ee_link relative to base_link):")  # Updated message
            self.get_logger().info(f"  Position (x,y,z): ({pos.x():.4f}, {pos.y():.4f}, {pos.z():.4f})")
            self.get_logger().info(f"  Orientation (r,p,y): ({rot[0]:.4f}, {rot[1]:.4f}, {rot[2]:.4f})")
        else:
            self.get_logger().warn(f"Initial FK calculation failed with code: {fk_result}")
            # Handle error appropriately, maybe raise exception or set a flag
            self.initial_frame = None # Indicate failure

        # 6) publisher and subscription
        self.joint_pub = self.create_publisher(JointState, 'ik_solution', 10)
        self.fk_pub = self.create_publisher(PoseStamped, 'fk_pose', 10)
        # self.command_pub = self.create_publisher(Float64MultiArray, '/maurice_arm/commands', 10)
        self.create_subscription(Twist, 'ik_delta', self.on_delta, 10)
        self.create_subscription(JointState, '/mars/arm/state', self.on_joint_states, 10)
        
        # Timer for FK publishing at 10Hz
        self.create_timer(0.1, self.publish_fk)  # 0.1 seconds = 10Hz
        
        # Store latest joint states
        self.latest_joint_states = None

        self.get_logger().info(f"KDL IK node ready (eps={eps}, maxiter={maxiter})")

    def on_joint_states(self, msg: JointState):
        """Store the latest joint states"""
        self.latest_joint_states = msg

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
            pose_msg.header.frame_id = 'base_link'
            
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

        # solve IK - seed with current_q, target target_frame
        q_out = kdl.JntArray(self.chain.getNrOfJoints())
        start_time = time.perf_counter() # Start timer
        # Use self.current_q as the initial guess (seed)
        ik_result = self.ik_solver.CartToJnt(self.current_q, target_frame, q_out)
        end_time = time.perf_counter() # End timer
        solve_time_ms = (end_time - start_time) * 1000 # Calculate duration in ms

        # Check the ik_result based on observed behavior and KDL definitions
        if ik_result >= 0:
            # Successful convergence (ik_result == SolverI.NoError, which is 0)
             self.get_logger().info(f'KDL IK solved successfully (took {solve_time_ms:.2f} ms)')
        elif ik_result == -100: # KDL::ChainIkSolver Pos_LMA::E_GRADIENT_JOINTS_TOO_SMALL
            # Gradient too small (local minimum / flat region) - accept result
            self.get_logger().warn(
                f'KDL IK gradient too small (solver returned {ik_result}), using approximate result. '
                f'(Took {solve_time_ms:.2f} ms)'
            )
            # Proceed with the potentially approximate result in q_out
        elif ik_result == -101: # KDL::ChainIkSolverPos_LMA::E_INCREMENT_JOINTS_TOO_SMALL
            # Joint increments too small - accept result
             self.get_logger().warn(
                f'KDL IK joint increments too small (solver returned {ik_result}), using approximate result. '
                f'(Took {solve_time_ms:.2f} ms)'
            )
             # Proceed with the potentially approximate result in q_out
        else:
            # Treat ALL other negative results (including max iterations exceeded) as unrecoverable errors
            self.get_logger().error(f'KDL IK failed with error code {ik_result} (took {solve_time_ms:.2f} ms)')
            return # Do not proceed

        # publish JointState
        js = JointState()
        js.header.stamp = self.get_clock().now().to_msg()
        js.name = self.joint_names
        js.position = [q_out[i] for i in range(q_out.rows())]
        self.joint_pub.publish(js)

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

if __name__ == '__main__':
    main()