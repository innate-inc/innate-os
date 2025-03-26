#!/usr/bin/env python3
import time
import threading
import numpy as np
import cv2  # Used for resizing images

import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Twist, TransformStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import JointState, Image
from std_msgs.msg import Float64MultiArray

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

        # Declare rendering resolution
        self.declare_parameter('rendering_resolution', 640)

        # Declare camera parameters individually instead of a nested dict
        self.declare_parameter('cameras.base.vertical_fov', 80.0)
        self.declare_parameter('cameras.base.aspect_ratio', 4.44)
        self.declare_parameter('cameras.base.resolution.width', 1280)
        self.declare_parameter('cameras.base.resolution.height', 800)
        self.declare_parameter('cameras.base.topic', '/camera_base/image_raw')

        self.declare_parameter('cameras.arm.vertical_fov', 150.0)
        self.declare_parameter('cameras.arm.aspect_ratio', 1.33)
        self.declare_parameter('cameras.arm.resolution.width', 640)
        self.declare_parameter('cameras.arm.resolution.height', 480)
        self.declare_parameter('cameras.arm.topic', '/camera_arm/image_raw')

        self.rendering_resolution = self.get_parameter('rendering_resolution').value

        # Assemble camera parameters into a nested dict for easier access later.
        self.camera_params = {
            'base': {
                'vertical_fov': self.get_parameter('cameras.base.vertical_fov').value,
                'aspect_ratio': self.get_parameter('cameras.base.aspect_ratio').value,
                'resolution': {
                    'width': self.get_parameter('cameras.base.resolution.width').value,
                    'height': self.get_parameter('cameras.base.resolution.height').value,
                },
                'topic': self.get_parameter('cameras.base.topic').value
            },
            'arm': {
                'vertical_fov': self.get_parameter('cameras.arm.vertical_fov').value,
                'aspect_ratio': self.get_parameter('cameras.arm.aspect_ratio').value,
                'resolution': {
                    'width': self.get_parameter('cameras.arm.resolution.width').value,
                    'height': self.get_parameter('cameras.arm.resolution.height').value,
                },
                'topic': self.get_parameter('cameras.arm.topic').value
            }
        }

        # --- Publishers & Subscribers ---
        self.odom_pub = self.create_publisher(Odometry, '/odom', 10)
        self.joint_state_pub = self.create_publisher(JointState, 'maurice_arm/state', 10)
        # Publishers for offscreen camera images
        self.camera_base_pub = self.create_publisher(Image, self.camera_params['base']['topic'], 10)
        self.camera_arm_pub  = self.create_publisher(Image, self.camera_params['arm']['topic'], 10)

        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, 10)
        self.create_subscription(Float64MultiArray, '/maurice_arm/commands', self.arm_commands_callback, 10)

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
        # Compute each camera's offscreen height using the fixed rendering_resolution (width)
        base_offscreen_height = int(self.rendering_resolution / self.camera_params['base']['aspect_ratio'])
        arm_offscreen_height = int(self.rendering_resolution / self.camera_params['arm']['aspect_ratio'])

        # Create a single offscreen GL context with the common width and maximum required height
        offscreen_width = self.rendering_resolution
        offscreen_height = max(base_offscreen_height, arm_offscreen_height)
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
        # Rely on MJCF settings; no need to override FOV here

        offscreen_camera_arm = mujoco.MjvCamera()
        mujoco.mjv_defaultCamera(offscreen_camera_arm)
        offscreen_camera_arm.type = mujoco.mjtCamera.mjCAMERA_FIXED
        offscreen_camera_arm.fixedcamid = camera_arm_id
        # Rely on MJCF settings; no need to override FOV here

        # Render offscreen images at ~30 Hz in this thread
        while self.viewer_handle.is_running():
            time.sleep(0.005)
            self.render_offscreen_images(mjr_context, mjv_scene, mjv_option,
                                             offscreen_camera_base, offscreen_camera_arm,
                                             base_offscreen_height, arm_offscreen_height)
        self.get_logger().info("Viewer closed.")

    def render_offscreen_images(self, mjr_context, mjv_scene, mjv_option,
                                camera_base, camera_arm, base_offscreen_height, arm_offscreen_height):
        start_time = time.time()
        offscreen_width = self.rendering_resolution

        # Create viewports for each camera using their computed offscreen dimensions
        viewport_base = mujoco.MjrRect(0, 0, offscreen_width, base_offscreen_height)
        viewport_arm  = mujoco.MjrRect(0, 0, offscreen_width, arm_offscreen_height)

        # ----- Render from the base camera -----
        t1 = time.time()
        mujoco.mjv_updateScene(self.model, self.data, mjv_option, None,
                               camera_base, mujoco.mjtCatBit.mjCAT_ALL, mjv_scene)
        t2 = time.time()
        mujoco.mjr_setBuffer(mujoco.mjtFramebuffer.mjFB_OFFSCREEN, mjr_context)
        mujoco.mjr_render(viewport_base, mjv_scene, mjr_context)
        t3 = time.time()
        img_base = np.empty((base_offscreen_height, offscreen_width, 3), dtype=np.uint8)
        mujoco.mjr_readPixels(img_base, None, viewport_base, mjr_context)
        t4 = time.time()

        # Resize the base image to the final resolution from parameters
        final_base_width = self.camera_params['base']['resolution']['width']
        final_base_height = self.camera_params['base']['resolution']['height']
        img_base_resized = cv2.resize(img_base, (final_base_width, final_base_height))
        # Flip the base image horizontally instead of rotating
        img_base_flipped = cv2.flip(img_base_resized, 0)  # 0 means vertical flip
        t5 = time.time()

        # Build and publish the ROS Image message for the base camera
        img_msg_base = Image()
        img_msg_base.header.stamp = self.get_clock().now().to_msg()
        img_msg_base.height = final_base_height
        img_msg_base.width = final_base_width
        img_msg_base.encoding = "rgb8"
        img_msg_base.is_bigendian = 0
        img_msg_base.step = final_base_width * 3
        img_msg_base.data = img_base_flipped.tobytes()
        t6 = time.time()
        self.camera_base_pub.publish(img_msg_base)
        t7 = time.time()

        # ----- Render from the arm camera -----
        mujoco.mjv_updateScene(self.model, self.data, mjv_option, None,
                               camera_arm, mujoco.mjtCatBit.mjCAT_ALL, mjv_scene)
        t8 = time.time()
        mujoco.mjr_render(viewport_arm, mjv_scene, mjr_context)
        t9 = time.time()
        img_arm = np.empty((arm_offscreen_height, offscreen_width, 3), dtype=np.uint8)
        mujoco.mjr_readPixels(img_arm, None, viewport_arm, mjr_context)
        t10 = time.time()

        # Resize the arm image to the final resolution from parameters
        final_arm_width = self.camera_params['arm']['resolution']['width']
        final_arm_height = self.camera_params['arm']['resolution']['height']
        img_arm_resized = cv2.resize(img_arm, (final_arm_width, final_arm_height))
        # Flip the arm image vertically instead of rotating 180 degrees
        img_arm_flipped = cv2.flip(img_arm_resized, 0)  # 0 means vertical flip
        t11 = time.time()

        img_msg_arm = Image()
        img_msg_arm.header.stamp = self.get_clock().now().to_msg()
        img_msg_arm.height = final_arm_height
        img_msg_arm.width = final_arm_width
        img_msg_arm.encoding = "rgb8"
        img_msg_arm.is_bigendian = 0
        img_msg_arm.step = final_arm_width * 3
        img_msg_arm.data = img_arm_flipped.tobytes()
        t12 = time.time()
        self.camera_arm_pub.publish(img_msg_arm)
        t13 = time.time()

        # Log timing information
        self.get_logger().info(f"Base camera: updateScene={1000*(t2-t1):.1f}ms, render={1000*(t3-t2):.1f}ms, " +
                              f"readPixels={1000*(t4-t3):.1f}ms, resize/flip={1000*(t5-t4):.1f}ms, " +
                              f"msg_prep={1000*(t6-t5):.1f}ms, publish={1000*(t7-t6):.1f}ms")
        
        self.get_logger().info(f"Arm camera: updateScene={1000*(t8-t7):.1f}ms, render={1000*(t9-t8):.1f}ms, " +
                              f"readPixels={1000*(t10-t9):.1f}ms, resize/flip={1000*(t11-t10):.1f}ms, " +
                              f"msg_prep={1000*(t12-t11):.1f}ms, publish={1000*(t13-t12):.1f}ms")
        
        self.get_logger().info(f"Total rendering time: {1000*(t13-start_time):.1f}ms")

    def cmd_vel_callback(self, msg: Twist):
        self.twist_cmd = msg
        self.get_logger().info(f"Received /cmd_vel: linear={msg.linear.x}, angular={msg.angular.z}")

    def arm_commands_callback(self, msg: Float64MultiArray):
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
