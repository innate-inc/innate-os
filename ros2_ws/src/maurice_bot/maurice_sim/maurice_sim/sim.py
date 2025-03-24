#!/usr/bin/env python3
import time
import threading

import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import JointState
from std_msgs.msg import Float32MultiArray

import mujoco
import numpy as np
from mujoco import viewer  # Import the viewer module

class MauriceBotNode(Node):
    def __init__(self):
        super().__init__('maurice_bot_node')

        self.declare_parameter('model_path', 'path/to/your/maurice_model.xml')
        model_path = self.get_parameter('model_path').value
        self.get_logger().info(f"Using model file: {model_path}")

        self.odom_pub = self.create_publisher(Odometry, '/odom', 10)
        self.joint_state_pub = self.create_publisher(JointState, 'maurice_arm/state', 10)

        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, 10)
        self.create_subscription(Float32MultiArray, '/maurice_arm/commands', self.arm_commands_callback, 10)

        self.simulation_timer = self.create_timer(0.002, self.simulation_timer_callback)
        self.publish_timer = self.create_timer(1.0 / 30.0, self.publish_timer_callback)

        self.model = mujoco.MjModel.from_xml_path(model_path)
        self.data = mujoco.MjData(self.model)

        self.twist_cmd = Twist()
        self.arm_commands = []

        self.viewer_handle = None
        self.viewer_thread = threading.Thread(target=self.launch_passive_viewer)
        self.viewer_thread.daemon = True
        self.viewer_thread.start()

    def launch_passive_viewer(self):
        self.viewer_handle = viewer.launch_passive(self.model, self.data)
        while self.viewer_handle.is_running():
            time.sleep(0.01)
        self.get_logger().info("Viewer closed.")

    def cmd_vel_callback(self, msg: Twist):
        self.twist_cmd = msg
        self.get_logger().info(f"Received /cmd_vel: linear={msg.linear.x}, angular={msg.angular.z}")

    def arm_commands_callback(self, msg: Float32MultiArray):
        self.arm_commands = msg.data
        self.get_logger().info(f"Received /maurice_arm/commands: {self.arm_commands}")

    def simulation_timer_callback(self):
        mujoco.mj_step(self.model, self.data)
        if self.viewer_handle is not None and self.viewer_handle.is_running():
            self.viewer_handle.sync()

    def publish_timer_callback(self):
        # Base control
        self.data.ctrl[0] = self.twist_cmd.linear.x     # base_x
        self.data.ctrl[1] = self.twist_cmd.angular.z    # base_yaw

        # Arm control
        if self.arm_commands:
            n = min(len(self.arm_commands), 6)
            self.data.ctrl[2:2+n] = self.arm_commands[:n]

        # Get global position and orientation of base_link
        base_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "base_link")
        pos = self.data.xpos[base_id]        # [x, y, z]
        mat = self.data.xmat[base_id].copy()  # 3x3 rotation matrix (flattened)

        # Calculate yaw angle from rotation matrix
        theta = np.arctan2(mat[3], mat[0])
        quat = self._mat_to_quat(mat)

        odom_msg = Odometry()
        odom_msg.header.stamp = self.get_clock().now().to_msg()
        odom_msg.header.frame_id = "odom"
        odom_msg.child_frame_id = "base_link"

        odom_msg.pose.pose.position.x = pos[0]
        odom_msg.pose.pose.position.y = pos[1]
        odom_msg.pose.pose.position.z = pos[2]
        odom_msg.pose.pose.orientation.x = quat[0]
        odom_msg.pose.pose.orientation.y = quat[1]
        odom_msg.pose.pose.orientation.z = quat[2]
        odom_msg.pose.pose.orientation.w = quat[3]

        # Base velocity from qvel
        vx = self.data.qvel[1]       # base_x
        vtheta = self.data.qvel[0]   # base_yaw
        odom_msg.twist.twist.linear.x = vx
        odom_msg.twist.twist.angular.z = vtheta

        self.odom_pub.publish(odom_msg)

        # Arm joint states
        joint_state_msg = JointState()
        joint_state_msg.header.stamp = self.get_clock().now().to_msg()
        joint_state_msg.name = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        if len(self.data.qpos) >= 8:
            joint_state_msg.position = self.data.qpos[2:8].tolist()
        else:
            joint_state_msg.position = [0.0] * 6

        self.joint_state_pub.publish(joint_state_msg)

    def _mat_to_quat(self, mat):
        """
        Convert a 3x3 rotation matrix (flattened) to quaternion [x, y, z, w]
        """
        # Create output array for the quaternion
        quat = np.zeros(4)
        # Use mujoco's function to convert matrix to quaternion
        mujoco.mju_mat2Quat(quat, mat)
        return quat.tolist()

def main(args=None):
    rclpy.init(args=args)
    node = MauriceBotNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass

    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()