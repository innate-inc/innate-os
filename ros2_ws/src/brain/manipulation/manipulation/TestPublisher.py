#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import cv2
from cv_bridge import CvBridge
import numpy as np

# Message types for image and sensor data
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray
from geometry_msgs.msg import Twist

class TestPublisher(Node):
    def __init__(self):
        super().__init__('test_publisher')
        self.bridge = CvBridge()
        
        # Hard-coded video file path; update this to your actual video file location.
        video_file = '/home/vignesh/maurice-prod/ros2_ws/src/brain/manipulation/manipulation/big_buck_bunny.mp4'
        self.cap = cv2.VideoCapture(video_file)
        if not self.cap.isOpened():
            self.get_logger().error(f"Could not open video file: {video_file}")
            rclpy.shutdown()
            return
        
        # Get the video's frame rate to set the timer rate.
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            fps = 30.0  # Default to 30 FPS if unavailable.
        
        # Create publishers for the image topics.
        self.pub_raw = self.create_publisher(Image, '/camera/image_raw', 10)
        self.pub_processed = self.create_publisher(Image, '/camera/image_processed', 10)
        
        # Create publisher for /arm/state (JointState).
        self.pub_arm_state = self.create_publisher(JointState, '/arm/state', 10)
        
        # Create publisher for /leader/command (Float64MultiArray).
        self.pub_leader_command = self.create_publisher(Float64MultiArray, '/leader/command', 10)
        
        # Create publisher for /cmd_vel (Twist).
        self.pub_cmd_vel = self.create_publisher(Twist, '/cmd_vel', 10)
        
        # Create a timer callback that publishes at the video's frame rate.
        self.timer = self.create_timer(1.0 / fps, self.timer_callback)
        self.get_logger().info("Test Publisher Node started, publishing video and dummy sensor data.")

    def timer_callback(self):
        # --- Publish video frames on /camera/image_raw and /camera/image_processed ---
        ret, frame = self.cap.read()
        if not ret:
            # Restart the video if it has ended.
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
            if not ret:
                self.get_logger().error("Failed to read video frame after reset.")
                return
        
        # Process the frame for the processed topic (convert to grayscale).
        processed_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        try:
            raw_msg = self.bridge.cv2_to_imgmsg(frame, encoding="bgr8")
            processed_msg = self.bridge.cv2_to_imgmsg(processed_frame, encoding="mono8")
        except Exception as e:
            self.get_logger().error(f"Error converting frame to image: {e}")
            return
        
        self.pub_raw.publish(raw_msg)
        self.pub_processed.publish(processed_msg)
        
        # --- Publish dummy sensor data on the other topics ---
        # JointState for /arm/state (e.g., simulating 6 joints)
        now = self.get_clock().now().to_msg()
        joint_state_msg = JointState()
        joint_state_msg.header.stamp = now
        joint_state_msg.name = [f"joint_{i+1}" for i in range(6)]
        joint_state_msg.position = [0.1 * (i+1) for i in range(6)]
        joint_state_msg.velocity = [0.05 * (i+1) for i in range(6)]
        joint_state_msg.effort = [0.0 for _ in range(6)]
        self.pub_arm_state.publish(joint_state_msg)
        
        # Leader command for /leader/command (dummy array of floats)
        leader_command_msg = Float64MultiArray()
        leader_command_msg.data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        self.pub_leader_command.publish(leader_command_msg)
        
        # Twist message for /cmd_vel (dummy linear and angular velocities)
        twist_msg = Twist()
        twist_msg.linear.x = 0.5
        twist_msg.linear.y = 0.0
        twist_msg.linear.z = 0.0
        twist_msg.angular.x = 0.0
        twist_msg.angular.y = 0.0
        twist_msg.angular.z = 0.1
        self.pub_cmd_vel.publish(twist_msg)
        
        self.get_logger().info("Published video frame and dummy sensor data.")

def main(args=None):
    rclpy.init(args=args)
    node = TestPublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Test Publisher Node shutting down.")
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
