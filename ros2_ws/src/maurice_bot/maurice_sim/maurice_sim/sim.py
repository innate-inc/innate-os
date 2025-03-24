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

        # Declare the model_path parameter with a default value.
        self.declare_parameter('model_path', 'path/to/your/maurice_model.xml')
        model_path = self.get_parameter('model_path').value
        self.get_logger().info(f"Using model file: {model_path}")

        # Publishers for odometry and joint states
        self.odom_pub = self.create_publisher(Odometry, '/odom', 10)
        self.joint_state_pub = self.create_publisher(JointState, 'maurice_arm/state', 10)

        # Subscribers for base twist and arm commands
        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, 10)
        self.create_subscription(Float32MultiArray, '/maurice_arm/commands', self.arm_commands_callback, 10)

        # Timer for simulation stepping at 0.002 sec (500 Hz)
        self.simulation_timer = self.create_timer(0.002, self.simulation_timer_callback)

        # Timer for publishing topics and setting control signals at 30 Hz
        self.publish_timer = self.create_timer(1.0 / 30.0, self.publish_timer_callback)

        # Load the MuJoCo model from the parameter provided.
        self.model = mujoco.MjModel.from_xml_path(model_path)
        self.data = mujoco.MjData(self.model)

        # Variables to hold the latest command messages
        self.twist_cmd = Twist()   # For base movement
        self.arm_commands = []     # For arm joints (float array)

        # Launch the passive viewer in a separate thread.
        self.viewer_handle = None
        self.viewer_thread = threading.Thread(target=self.launch_passive_viewer)
        self.viewer_thread.daemon = True  # Ensures thread exits with the node.
        self.viewer_thread.start()

    def launch_passive_viewer(self):
        """
        Launch the passive viewer which returns a viewer handle.
        This viewer will update its display when sync() is called.
        """
        self.viewer_handle = viewer.launch_passive(self.model, self.data)
        # Keep the thread alive while the viewer window is open.
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
        # Advance the simulation by one internal timestep (0.002 sec)
        mujoco.mj_step(self.model, self.data)
        # Update the viewer display by synchronizing its state.
        if self.viewer_handle is not None and self.viewer_handle.is_running():
            self.viewer_handle.sync()

    def publish_timer_callback(self):
        # --- Set control values based on received commands ---
        if self.arm_commands:
            n = min(len(self.arm_commands), 6)
            self.data.ctrl[-n:] = self.arm_commands[:n]

        # --- Publish odometry ---
        odom_msg = Odometry()
        odom_msg.header.stamp = self.get_clock().now().to_msg()
        odom_msg.header.frame_id = "odom"
        odom_msg.child_frame_id = "base_link"

        # Extract position and orientation from qpos (adjust indices as needed)
        if len(self.data.qpos) >= 7:
            pos = self.data.qpos[:3]
            quat = self.data.qpos[3:7]
        else:
            pos = [0.0, 0.0, 0.0]
            quat = [1.0, 0.0, 0.0, 0.0]
        odom_msg.pose.pose.position.x = pos[0]
        odom_msg.pose.pose.position.y = pos[1]
        odom_msg.pose.pose.position.z = pos[2]
        odom_msg.pose.pose.orientation.w = quat[0]
        odom_msg.pose.pose.orientation.x = quat[1]
        odom_msg.pose.pose.orientation.y = quat[2]
        odom_msg.pose.pose.orientation.z = quat[3]
        self.odom_pub.publish(odom_msg)

        # --- Publish joint states for the arm ---
        joint_state_msg = JointState()
        joint_state_msg.header.stamp = self.get_clock().now().to_msg()
        joint_state_msg.name = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        if len(self.data.qpos) >= 13:
            joint_state_msg.position = self.data.qpos[7:13].tolist()
        else:
            joint_state_msg.position = [0.0] * 6
        self.joint_state_pub.publish(joint_state_msg)

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
