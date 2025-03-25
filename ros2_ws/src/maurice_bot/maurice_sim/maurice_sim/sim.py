#!/usr/bin/env python3
import time
import threading
import numpy as np

import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Twist, TransformStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import JointState, Image
from std_msgs.msg import Float32MultiArray

import tf2_ros

import mujoco
from mujoco import viewer

class MauriceBotNode(Node):
    def __init__(self):
        super().__init__('maurice_bot_node')

        # --- Parameters & Logging ---
        self.declare_parameter('model_path', 'path/to/your/maurice_model.xml')
        model_path = self.get_parameter('model_path').value
        self.get_logger().info(f"Using model file: {model_path}")

        # --- Publishers & Subscribers ---
        self.odom_pub = self.create_publisher(Odometry, '/odom', 10)
        self.joint_state_pub = self.create_publisher(JointState, 'maurice_arm/state', 10)
        # Publishers for offscreen camera images
        self.camera_base_pub = self.create_publisher(Image, '/camera_base/image_raw', 10)
        self.camera_arm_pub  = self.create_publisher(Image, '/camera_arm/image_raw', 10)

        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, 10)
        self.create_subscription(Float32MultiArray, '/maurice_arm/commands', self.arm_commands_callback, 10)

        # --- Timers for simulation and publishing ---
        self.simulation_timer = self.create_timer(0.002, self.simulation_timer_callback)
        self.publish_timer = self.create_timer(1.0 / 30.0, self.publish_timer_callback)

        # --- Load Mujoco model and data ---
        self.model = mujoco.MjModel.from_xml_path(model_path)
        self.data = mujoco.MjData(self.model)

        self.twist_cmd = Twist()
        self.arm_commands = []

        # --- TF Broadcaster ---
        self.tf_broadcaster = tf2_ros.TransformBroadcaster(self)

        # --- Launch the Passive Viewer Thread ---
        self.viewer_thread = threading.Thread(target=self.launch_passive_viewer)
        self.viewer_thread.daemon = True
        self.viewer_thread.start()

    def launch_passive_viewer(self):
        # Launch the passive viewer (creates its own window/context)
        self.viewer_handle = viewer.launch_passive(self.model, self.data)

        # --- Offscreen Rendering Setup (executed in the viewer thread) ---
        offscreen_width = 640
        offscreen_height = 480

        # Create an offscreen GL context and make it current in this thread
        offscreen_ctx = mujoco.GLContext(offscreen_width, offscreen_height)
        offscreen_ctx.make_current()

        # Create the offscreen rendering context, scene, and visualization options
        mjr_context = mujoco.MjrContext(self.model, mujoco.mjtFontScale.mjFONTSCALE_100)
        mjv_scene = mujoco.MjvScene(self.model, maxgeom=1000)
        mjv_option = mujoco.MjvOption()

        # Retrieve camera IDs using their names from the model
        camera_base_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, "camera_base")
        camera_arm_id  = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, "camera_arm")

        # Create two mjvCamera objects for offscreen rendering (fixed cameras)
        offscreen_camera_base = mujoco.MjvCamera()
        mujoco.mjv_defaultCamera(offscreen_camera_base)
        offscreen_camera_base.type = mujoco.mjtCamera.mjCAMERA_FIXED
        offscreen_camera_base.fixedcamid = camera_base_id

        offscreen_camera_arm = mujoco.MjvCamera()
        mujoco.mjv_defaultCamera(offscreen_camera_arm)
        offscreen_camera_arm.type = mujoco.mjtCamera.mjCAMERA_FIXED
        offscreen_camera_arm.fixedcamid = camera_arm_id

        # Render offscreen images at ~30 Hz in this thread
        last_time = time.time()
        rate = 1.0 / 30.0
        while self.viewer_handle.is_running():
            time.sleep(0.005)
            now = time.time()
            if now - last_time >= rate:
                last_time = now
                self.render_offscreen_images(offscreen_width, offscreen_height,
                                             mjr_context, mjv_scene, mjv_option,
                                             offscreen_camera_base, offscreen_camera_arm)
        self.get_logger().info("Viewer closed.")

    def render_offscreen_images(self, width, height, mjr_context, mjv_scene, mjv_option,
                                camera_base, camera_arm):
        # Create a viewport covering the entire offscreen area
        viewport = mujoco.MjrRect(0, 0, width, height)

        # ----- Render from the base camera -----
        mujoco.mjv_updateScene(self.model, self.data, mjv_option, None,
                               camera_base, mujoco.mjtCatBit.mjCAT_ALL, mjv_scene)
        mujoco.mjr_setBuffer(mujoco.mjtFramebuffer.mjFB_OFFSCREEN, mjr_context)
        mujoco.mjr_render(viewport, mjv_scene, mjr_context)
        img_base = np.empty((height, width, 3), dtype=np.uint8)
        mujoco.mjr_readPixels(img_base, None, viewport, mjr_context)

        # Convert and publish the base camera image as a ROS Image message
        img_msg_base = Image()
        img_msg_base.header.stamp = self.get_clock().now().to_msg()
        img_msg_base.height = height
        img_msg_base.width = width
        img_msg_base.encoding = "rgb8"
        img_msg_base.is_bigendian = 0
        img_msg_base.step = width * 3
        img_msg_base.data = img_base.tobytes()
        self.get_logger().info(f"Published base camera image: {img_msg_base.header.stamp}")
        self.camera_base_pub.publish(img_msg_base)

        # ----- Render from the arm camera -----
        mujoco.mjv_updateScene(self.model, self.data, mjv_option, None,
                               camera_arm, mujoco.mjtCatBit.mjCAT_ALL, mjv_scene)
        mujoco.mjr_render(viewport, mjv_scene, mjr_context)
        img_arm = np.empty((height, width, 3), dtype=np.uint8)
        mujoco.mjr_readPixels(img_arm, None, viewport, mjr_context)
        img_msg_arm = Image()
        img_msg_arm.header.stamp = self.get_clock().now().to_msg()
        img_msg_arm.height = height
        img_msg_arm.width = width
        img_msg_arm.encoding = "rgb8"
        img_msg_arm.is_bigendian = 0
        img_msg_arm.step = width * 3
        img_msg_arm.data = img_arm.tobytes()
        self.get_logger().info(f"Published arm camera image: {img_msg_arm.header.stamp}")
        self.camera_arm_pub.publish(img_msg_arm)

    def cmd_vel_callback(self, msg: Twist):
        self.twist_cmd = msg
        self.get_logger().info(f"Received /cmd_vel: linear={msg.linear.x}, angular={msg.angular.z}")

    def arm_commands_callback(self, msg: Float32MultiArray):
        self.arm_commands = msg.data
        self.get_logger().info(f"Received /maurice_arm/commands: {self.arm_commands}")

    def simulation_timer_callback(self):
        mujoco.mj_step(self.model, self.data)
        if hasattr(self, 'viewer_handle') and self.viewer_handle is not None and self.viewer_handle.is_running():
            self.viewer_handle.sync()

    def publish_timer_callback(self):
        # ---- Base Control ----
        current_yaw = self.data.qpos[2]
        v_cmd = self.twist_cmd.linear.x
        vx_world = v_cmd * np.cos(current_yaw)
        vy_world = v_cmd * np.sin(current_yaw)

        self.data.ctrl[0] = vx_world
        self.data.ctrl[1] = vy_world
        self.data.ctrl[2] = self.twist_cmd.angular.z

        # ---- Arm Control ----
        if self.arm_commands:
            n = min(len(self.arm_commands), 6)
            self.data.ctrl[3:3+n] = self.arm_commands[:n]

        # ---- Odometry Publishing ----
        odom_msg = Odometry()
        odom_msg.header.stamp = self.get_clock().now().to_msg()
        odom_msg.header.frame_id = "odom"
        odom_msg.child_frame_id = "base_link"
        odom_msg.pose.pose.position.x = self.data.qpos[0]
        odom_msg.pose.pose.position.y = self.data.qpos[1]
        odom_msg.pose.pose.position.z = 0.0

        yaw = self.data.qpos[2]
        qz = np.sin(yaw / 2.0)
        qw = np.cos(yaw / 2.0)
        odom_msg.pose.pose.orientation.z = qz
        odom_msg.pose.pose.orientation.w = qw

        odom_msg.twist.twist.linear.x = self.data.qvel[0]
        odom_msg.twist.twist.linear.y = self.data.qvel[1]
        odom_msg.twist.twist.angular.z = self.data.qvel[2]
        self.odom_pub.publish(odom_msg)

        # ---- Publish TF from "odom" to "base_link" ----
        transform = TransformStamped()
        transform.header.stamp = self.get_clock().now().to_msg()
        transform.header.frame_id = "odom"
        transform.child_frame_id = "base_link"
        transform.transform.translation.x = self.data.qpos[0]
        transform.transform.translation.y = self.data.qpos[1]
        transform.transform.translation.z = 0.0
        transform.transform.rotation.z = qz
        transform.transform.rotation.w = qw
        self.tf_broadcaster.sendTransform(transform)

        # ---- Arm Joint States Publishing ----
        joint_state_msg = JointState()
        joint_state_msg.header.stamp = self.get_clock().now().to_msg()
        joint_state_msg.name = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        if len(self.data.qpos) >= 9:
            joint_state_msg.position = self.data.qpos[3:9].tolist()
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
