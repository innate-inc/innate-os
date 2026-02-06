#!/usr/bin/env python3
"""
Stereo Camera Recorder

Records left and right feeds from the stereo camera to separate MP4 files.
Subscribes to /mars/main_camera/stereo (side-by-side 1280x480 image).

Usage:
    # First, make sure the camera driver is running
    ros2 run maurice_cam record_stereo
    
    # Or run directly:
    python3 record_stereo.py --duration 30 --output-dir /path/to/output
    
    # Press Ctrl+C to stop recording early
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import Image
from cv_bridge import CvBridge


class StereoRecorder(Node):
    def __init__(
        self,
        output_dir: str,
        duration: float = 0,
        fps: float = 30.0,
        stereo_topic: str = "/mars/main_camera/stereo",
    ):
        super().__init__("stereo_recorder")
        
        self.bridge = CvBridge()
        self.fps = fps
        self.duration = duration
        self.frame_count = 0
        self.start_time = None
        self.recording = True
        
        # Create output directory with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = Path(output_dir) / f"stereo_recording_{timestamp}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Output files
        self.left_file = self.output_dir / "left.mp4"
        self.right_file = self.output_dir / "right.mp4"
        
        # Video writers (initialized on first frame)
        self.left_writer = None
        self.right_writer = None
        self.fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        
        # QoS profile for camera topics
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Subscribe to stereo topic
        self.subscription = self.create_subscription(
            Image,
            stereo_topic,
            self.stereo_callback,
            qos
        )
        
        self.get_logger().info(f"Stereo Recorder initialized")
        self.get_logger().info(f"  Output directory: {self.output_dir}")
        self.get_logger().info(f"  Subscribing to: {stereo_topic}")
        self.get_logger().info(f"  Target FPS: {fps}")
        if duration > 0:
            self.get_logger().info(f"  Duration: {duration} seconds")
        else:
            self.get_logger().info(f"  Duration: unlimited (Ctrl+C to stop)")
        self.get_logger().info("Waiting for frames...")

    def stereo_callback(self, msg: Image):
        if not self.recording:
            return
            
        try:
            # Convert ROS image to OpenCV
            if msg.encoding == "bgr8":
                stereo_frame = self.bridge.imgmsg_to_cv2(msg, "bgr8")
            elif msg.encoding == "rgb8":
                stereo_frame = self.bridge.imgmsg_to_cv2(msg, "rgb8")
                stereo_frame = cv2.cvtColor(stereo_frame, cv2.COLOR_RGB2BGR)
            else:
                self.get_logger().warn(f"Unsupported encoding: {msg.encoding}, trying passthrough")
                stereo_frame = self.bridge.imgmsg_to_cv2(msg, "passthrough")
                if len(stereo_frame.shape) == 2:
                    stereo_frame = cv2.cvtColor(stereo_frame, cv2.COLOR_GRAY2BGR)
            
            # Split into left and right
            height, width = stereo_frame.shape[:2]
            half_width = width // 2
            
            left_frame = stereo_frame[:, :half_width]
            right_frame = stereo_frame[:, half_width:]
            
            # Initialize writers on first frame
            if self.left_writer is None:
                frame_size = (half_width, height)
                self.left_writer = cv2.VideoWriter(
                    str(self.left_file), self.fourcc, self.fps, frame_size
                )
                self.right_writer = cv2.VideoWriter(
                    str(self.right_file), self.fourcc, self.fps, frame_size
                )
                self.start_time = self.get_clock().now()
                self.get_logger().info(f"Recording started! Frame size: {frame_size}")
            
            # Write frames
            self.left_writer.write(left_frame)
            self.right_writer.write(right_frame)
            self.frame_count += 1
            
            # Log progress every 30 frames
            if self.frame_count % 30 == 0:
                elapsed = (self.get_clock().now() - self.start_time).nanoseconds / 1e9
                actual_fps = self.frame_count / elapsed if elapsed > 0 else 0
                self.get_logger().info(
                    f"Recorded {self.frame_count} frames ({elapsed:.1f}s, {actual_fps:.1f} FPS)"
                )
            
            # Check duration limit
            if self.duration > 0:
                elapsed = (self.get_clock().now() - self.start_time).nanoseconds / 1e9
                if elapsed >= self.duration:
                    self.stop_recording()
                    
        except Exception as e:
            self.get_logger().error(f"Error processing frame: {e}")

    def stop_recording(self):
        if not self.recording:
            return
            
        self.recording = False
        
        if self.left_writer is not None:
            self.left_writer.release()
            self.right_writer.release()
            
            elapsed = (self.get_clock().now() - self.start_time).nanoseconds / 1e9
            actual_fps = self.frame_count / elapsed if elapsed > 0 else 0
            
            self.get_logger().info("=" * 50)
            self.get_logger().info("Recording complete!")
            self.get_logger().info(f"  Total frames: {self.frame_count}")
            self.get_logger().info(f"  Duration: {elapsed:.2f} seconds")
            self.get_logger().info(f"  Average FPS: {actual_fps:.1f}")
            self.get_logger().info(f"  Left video:  {self.left_file}")
            self.get_logger().info(f"  Right video: {self.right_file}")
            self.get_logger().info("=" * 50)
        else:
            self.get_logger().warn("No frames were recorded!")
        
        # Shutdown
        rclpy.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Record stereo camera feeds to MP4 files")
    parser.add_argument(
        "--output-dir", "-o",
        default="/home/jetson1/innate-os/data/recordings",
        help="Output directory for recordings"
    )
    parser.add_argument(
        "--duration", "-d",
        type=float,
        default=0,
        help="Recording duration in seconds (0 = unlimited)"
    )
    parser.add_argument(
        "--fps", "-f",
        type=float,
        default=30.0,
        help="Target FPS for output video"
    )
    parser.add_argument(
        "--topic", "-t",
        default="/mars/main_camera/stereo",
        help="Stereo image topic to subscribe to"
    )
    
    args = parser.parse_args()
    
    rclpy.init()
    
    recorder = StereoRecorder(
        output_dir=args.output_dir,
        duration=args.duration,
        fps=args.fps,
        stereo_topic=args.topic,
    )
    
    try:
        rclpy.spin(recorder)
    except KeyboardInterrupt:
        recorder.get_logger().info("Keyboard interrupt received, stopping...")
        recorder.stop_recording()
    finally:
        if rclpy.ok():
            recorder.destroy_node()
            rclpy.shutdown()


if __name__ == "__main__":
    main()
