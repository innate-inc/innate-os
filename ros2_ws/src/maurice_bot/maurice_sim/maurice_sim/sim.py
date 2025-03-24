#!/usr/bin/env python3
import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import JointState
from std_msgs.msg import Float32MultiArray

# Import MuJoCo (using mujoco_py as an example)
import mujoco
import numpy as np

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

        # Timer for rendering the simulation at 10 Hz
        self.viewer_timer = self.create_timer(1.0 / 10.0, self.viewer_timer_callback)

        # Load the MuJoCo model from the parameter provided.
        self.model = mujoco.MjModel.from_xml_path(model_path)
        self.data = mujoco.MjData(self.model)

        # Create a viewer for rendering the simulation.
        # Note: This requires a valid display environment.
        self.viewer = mujoco.Viewer(self.model, self.data)

        # Variables to hold the latest command messages
        self.twist_cmd = Twist()   # For base movement
        self.arm_commands = []     # For arm joints (float array)

    def cmd_vel_callback(self, msg: Twist):
        self.twist_cmd = msg
        self.get_logger().info(f"Received /cmd_vel: linear={msg.linear.x}, angular={msg.angular.z}")

    def arm_commands_callback(self, msg: Float32MultiArray):
        self.arm_commands = msg.data
        self.get_logger().info(f"Received /maurice_arm/commands: {self.arm_commands}")

    def simulation_timer_callback(self):
        # Advance the simulation by one internal timestep (0.002 sec)
        mujoco.mj_step(self.model, self.data)

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

        # Extract position and orientation from qpos (adjust indices to your model)
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

    def viewer_timer_callback(self):
        # Render the simulation at 10 Hz.
        # This updates the display window.
        self.viewer.render()

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
