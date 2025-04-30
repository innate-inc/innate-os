#!/usr/bin/env python3
"""
KDL-based IK node loading URDF directly from maurice_sim and using the package-local URDF→KDL parser (urdf.py).
"""
import os
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from sensor_msgs.msg import JointState
from urdf_parser_py.urdf import URDF
from maurice_arm.urdf import treeFromUrdfModel  # local parser in urdf.py
import PyKDL as kdl
from ament_index_python.packages import get_package_share_directory

class KDLIKNode(Node):
    def __init__(self):
        super().__init__('kdl_ik_from_file')
        # 1) declare & read solver parameters
        self.declare_parameter('search_resolution', 0.005)
        self.declare_parameter('timeout', 0.1)
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
        tip_link  = 'link5'
        self.chain = tree.getChain(base_link, tip_link)

        # 4) FK and IK solver setup
        self.fksolver  = kdl.ChainFkSolverPos_recursive(self.chain)
        self.ik_solver = kdl.ChainIkSolverPos_LMA(self.chain, eps=eps, maxiter=maxiter)

        # 5) prepare joint array and names
        nj = self.chain.getNrOfJoints()
        self.current_q = kdl.JntArray(nj)

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

        # 6) publisher and subscription
        self.joint_pub = self.create_publisher(JointState, 'ik_solution', 10)
        self.create_subscription(Twist, 'ik_delta', self.on_delta, 10)

        # Calculate and print initial FK pose
        initial_fk_frame = kdl.Frame()
        fk_result = self.fksolver.JntToCart(self.current_q, initial_fk_frame)
        if fk_result >= 0:
            pos = initial_fk_frame.p
            rot = initial_fk_frame.M.GetRPY()
            self.get_logger().info(f"Initial FK pose (link5 relative to base_link):")
            self.get_logger().info(f"  Position (x,y,z): ({pos.x():.4f}, {pos.y():.4f}, {pos.z():.4f})")
            self.get_logger().info(f"  Orientation (r,p,y): ({rot[0]:.4f}, {rot[1]:.4f}, {rot[2]:.4f})")
        else:
            self.get_logger().warn(f"Initial FK calculation failed with code: {fk_result}")

        self.get_logger().info(f"KDL IK node ready (eps={eps}, maxiter={maxiter})")

    def on_delta(self, delta: Twist):
        # forward kinematics
        end_frame = kdl.Frame()
        self.fksolver.JntToCart(self.current_q, end_frame)

        # apply delta position
        end_frame.p.x(end_frame.p.x() + delta.linear.x)
        end_frame.p.y(end_frame.p.y() + delta.linear.y)
        end_frame.p.z(end_frame.p.z() + delta.linear.z)
        # orientation deltas can be added here if needed

        # solve IK
        q_out = kdl.JntArray(self.chain.getNrOfJoints())
        if self.ik_solver.CartToJnt(self.current_q, end_frame, q_out) < 0:
            self.get_logger().warn('KDL IK failed')
            return

        # publish JointState
        js = JointState()
        js.header.stamp = self.get_clock().now().to_msg()
        js.name = self.joint_names
        js.position = [q_out[i] for i in range(q_out.rows())]
        self.joint_pub.publish(js)

        # seed next solve
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
