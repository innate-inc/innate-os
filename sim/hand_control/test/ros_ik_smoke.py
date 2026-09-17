"""Run inside a network-isolated Innate runtime; exercises the actual ROS node."""

import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws/src/mars_bot/mars_arm"))
sys.path.insert(0, str(REPO / "sim/hand_control"))

import PyKDL as kdl  # noqa: E402
import rclpy  # noqa: E402
from geometry_msgs.msg import Twist  # noqa: E402
from ik_worker import ROBOT_URDF  # noqa: E402

from mars_arm import ik  # noqa: E402


class Capture:
    message = None

    def publish(self, message):
        self.message = message


ik.get_package_share_directory = lambda name: str(ROBOT_URDF.parent.parent)
rclpy.init()
node = ik.KDLIKNode()
try:
    node.joint_pub = Capture()
    node.ik_fk_pub = Capture()
    node.current_q = kdl.JntArray(5)
    for i, value in enumerate([0, 0.15, 0.4, -0.55, 0]):
        node.current_q[i] = value
    target = Twist()
    target.linear.x, target.linear.y, target.linear.z = 0.35, -0.05285, 0.13
    target.angular.x = 0.4
    node.on_delta(target)
    joints = node.joint_pub.message
    assert joints is not None and len(joints.position) == 5
    assert abs(joints.position[4] - 0.4) < 0.01
    pose = node.ik_fk_pub.message.pose.position
    assert math.dist([pose.x, pose.y, pose.z], [0.35, -0.05285, 0.13]) < 0.001
    for i, value in enumerate(joints.position):
        assert abs(node.current_q[i] - value) < 1e-8
    print("Actual Innate ROS IK node: five-joint output, FK, and next-solve seed passed.")
finally:
    node.destroy_node()
    rclpy.shutdown()
