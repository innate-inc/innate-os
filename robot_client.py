#!/usr/bin/env python3
"""
Robot Waypoint Client - Streams sensor data and executes waypoint commands.

Runs on the robot and:
1. Subscribes to camera, odometry, and head position ROS topics
2. Streams sensor data to inference server over WebSocket
3. Receives waypoint commands from server
4. Executes waypoints using Nav2 FollowPath action

Usage:
    ros2 run waypoint_client robot_client --ros-args -p server_address:=<server_ip>
    
    Or directly:
    python robot_client.py --server_address <server_ip>
"""

import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

import asyncio
import websockets
import json
import threading
import time
import base64
import math
import numpy as np
from typing import Optional, List, Dict
from collections import deque

from sensor_msgs.msg import CompressedImage
from nav_msgs.msg import Odometry, Path
from geometry_msgs.msg import PoseStamped, Twist
from std_msgs.msg import String

import sys
from pathlib import Path as FilePath
sys.path.insert(0, str(FilePath(__file__).parent))

from shared_protocol import (
    SensorData, WaypointCommand, Waypoint, TaskStart, TaskStop, Kill,
    Status, Heartbeat, Odometry as OdomProto, parse_message,
    MSG_TYPE_WAYPOINT_COMMAND, MSG_TYPE_TASK_START, MSG_TYPE_TASK_STOP,
    MSG_TYPE_KILL, DEFAULT_INFERENCE_PORT
)


def quaternion_to_yaw(x: float, y: float, z: float, w: float) -> float:
    """Convert quaternion to yaw angle (radians)."""
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(siny_cosp, cosy_cosp)


def yaw_to_quaternion(yaw: float) -> tuple:
    """Convert yaw angle to quaternion (x, y, z, w)."""
    return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))


class DirectWaypointExecutor:
    """
    Direct waypoint execution bypassing Nav2.
    Publishes directly to /cmd_vel using simple proportional control.
    """
    
    # Control parameters
    LINEAR_SPEED = 0.3          # m/s forward speed
    ANGULAR_SPEED = 0.6         # rad/s max angular speed
    DISTANCE_TOLERANCE = 0.15   # m - consider waypoint reached
    ANGLE_TOLERANCE = 0.15      # rad - angle considered aligned (~8.5 deg)
    TURN_FIRST_THRESHOLD = 0.4  # rad - if angle error > this, turn in place first
    
    def __init__(self, node: Node):
        self.node = node
        self.logger = node.get_logger()
        
        # Publisher for direct velocity commands
        self.cmd_vel_pub = node.create_publisher(Twist, '/cmd_vel', 10)
        
        # Current execution state
        self.is_executing = False
        self.current_waypoints: List[Waypoint] = []
        self.current_waypoint_idx = 0
        
        # Robot pose (updated externally)
        self.robot_x = 0.0
        self.robot_y = 0.0
        self.robot_yaw = 0.0
        
        # Lock for thread safety
        self.lock = threading.Lock()
        
        # Control timer (20Hz control loop)
        self.control_timer = node.create_timer(0.05, self._control_loop)
    
    def update_waypoints(self, waypoints: List[Waypoint], robot_x: float, robot_y: float, robot_yaw: float):
        """
        Update waypoints to follow.
        
        Args:
            waypoints: List of Waypoint objects in odom frame
            robot_x, robot_y, robot_yaw: Current robot pose
        """
        with self.lock:
            self.current_waypoints = waypoints
            self.current_waypoint_idx = 0
            self.robot_x = robot_x
            self.robot_y = robot_y
            self.robot_yaw = robot_yaw
            
            if waypoints:
                self.is_executing = True
                wp = waypoints[0]
                self.logger.info(f"Direct waypoint executor: {len(waypoints)} waypoints, first=({wp.x:.2f}, {wp.y:.2f})")
            else:
                self.is_executing = False
    
    def update_pose(self, robot_x: float, robot_y: float, robot_yaw: float):
        """Update current robot pose from odometry."""
        with self.lock:
            self.robot_x = robot_x
            self.robot_y = robot_y
            self.robot_yaw = robot_yaw
    
    def _normalize_angle(self, angle: float) -> float:
        """Normalize angle to [-pi, pi]."""
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle
    
    def _control_loop(self):
        """Main control loop - compute and publish cmd_vel."""
        with self.lock:
            if not self.is_executing or not self.current_waypoints:
                return
            
            if self.current_waypoint_idx >= len(self.current_waypoints):
                # All waypoints reached
                self._stop_robot()
                self.is_executing = False
                self.logger.info("All waypoints reached!")
                return
            
            # Get current target waypoint
            wp = self.current_waypoints[self.current_waypoint_idx]
            
            # Compute error to waypoint
            dx = wp.x - self.robot_x
            dy = wp.y - self.robot_y
            distance = math.sqrt(dx*dx + dy*dy)
            
            # Check if waypoint reached
            if distance < self.DISTANCE_TOLERANCE:
                self.current_waypoint_idx += 1
                self.logger.info(f"Waypoint {self.current_waypoint_idx} reached, distance={distance:.3f}m")
                if self.current_waypoint_idx >= len(self.current_waypoints):
                    self._stop_robot()
                    self.is_executing = False
                    self.logger.info("All waypoints completed!")
                return
            
            # Compute desired heading
            desired_yaw = math.atan2(dy, dx)
            angle_error = self._normalize_angle(desired_yaw - self.robot_yaw)
            
            # Compute velocity command
            twist = Twist()
            
            if abs(angle_error) > self.TURN_FIRST_THRESHOLD:
                # Large angle error - turn in place first
                twist.linear.x = 0.0
                twist.angular.z = self.ANGULAR_SPEED * (1.0 if angle_error > 0 else -1.0)
            else:
                # Drive towards waypoint with proportional angular correction
                twist.linear.x = self.LINEAR_SPEED
                # Proportional angular control
                twist.angular.z = 2.0 * angle_error  # P-gain of 2.0
                # Clamp angular velocity
                twist.angular.z = max(-self.ANGULAR_SPEED, min(self.ANGULAR_SPEED, twist.angular.z))
            
            self.cmd_vel_pub.publish(twist)
    
    def _stop_robot(self):
        """Send zero velocity command."""
        twist = Twist()
        self.cmd_vel_pub.publish(twist)
    
    def stop(self):
        """Stop current execution."""
        with self.lock:
            self.is_executing = False
            self.current_waypoints = []
            self.current_waypoint_idx = 0
        self._stop_robot()
        self.logger.info("Waypoint execution stopped")


# Keep old class name as alias for compatibility
WaypointExecutor = DirectWaypointExecutor


class RobotWaypointClient(Node):
    """ROS2 node that streams sensor data and executes waypoints."""
    
    def __init__(self):
        super().__init__('robot_waypoint_client')
        
        # Parameters
        self.declare_parameter('server_address', '192.168.1.100')
        self.declare_parameter('server_port', DEFAULT_INFERENCE_PORT)
        self.declare_parameter('camera_topic', '/mars/main_camera/image/compressed')
        self.declare_parameter('odom_topic', '/odom')
        self.declare_parameter('head_position_topic', '/head_servo/status')
        self.declare_parameter('stream_hz', 10.0)  # Sensor streaming rate
        
        self.server_address = self.get_parameter('server_address').value
        self.server_port = self.get_parameter('server_port').value
        self.camera_topic = self.get_parameter('camera_topic').value
        self.odom_topic = self.get_parameter('odom_topic').value
        self.head_position_topic = self.get_parameter('head_position_topic').value
        self.stream_hz = self.get_parameter('stream_hz').value
        
        # State
        self.latest_image: Optional[CompressedImage] = None
        self.latest_odom: Optional[Odometry] = None
        self.latest_head_pitch: float = -10.0  # Default pitch
        self.seq = 0
        
        # Task state
        self.task_active = False
        self.task_label = ""
        self.killed = False
        
        # WebSocket connection
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.ws_connected = False
        self.ws_loop = None
        
        # Waypoint executor
        self.waypoint_executor = WaypointExecutor(self)
        
        # QoS for camera (best effort for compressed images)
        image_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Subscribers
        self.image_sub = self.create_subscription(
            CompressedImage,
            self.camera_topic,
            self.image_callback,
            image_qos
        )
        
        self.odom_sub = self.create_subscription(
            Odometry,
            self.odom_topic,
            self.odom_callback,
            10
        )
        
        if self.head_position_topic:
            self.head_sub = self.create_subscription(
                String,
                self.head_position_topic,
                self.head_callback,
                10
            )
        
        # Timer for streaming sensor data
        self.stream_timer = self.create_timer(1.0 / self.stream_hz, self.stream_callback)
        
        # Start WebSocket connection in background
        self.ws_thread = threading.Thread(target=self.run_websocket_client, daemon=True)
        self.ws_thread.start()
        
        self.get_logger().info(f"Robot Waypoint Client initialized")
        self.get_logger().info(f"  Server: {self.server_address}:{self.server_port}")
        self.get_logger().info(f"  Camera: {self.camera_topic}")
        self.get_logger().info(f"  Odom: {self.odom_topic}")
        self.get_logger().info(f"  Stream rate: {self.stream_hz} Hz")
    
    def image_callback(self, msg: CompressedImage):
        """Handle incoming camera image."""
        if self.latest_image is None:
            self.get_logger().info(f"✓ First camera frame received ({len(msg.data)} bytes)")
        self.latest_image = msg
    
    def odom_callback(self, msg: Odometry):
        """Handle incoming odometry."""
        if self.latest_odom is None:
            self.get_logger().info(
                f"✓ First odom received: x={msg.pose.pose.position.x:.3f}, "
                f"y={msg.pose.pose.position.y:.3f}"
            )
        self.latest_odom = msg
        
        # Update waypoint executor with current pose for direct control
        robot_x = msg.pose.pose.position.x
        robot_y = msg.pose.pose.position.y
        robot_yaw = quaternion_to_yaw(
            msg.pose.pose.orientation.x,
            msg.pose.pose.orientation.y,
            msg.pose.pose.orientation.z,
            msg.pose.pose.orientation.w
        )
        self.waypoint_executor.update_pose(robot_x, robot_y, robot_yaw)
    
    def head_callback(self, msg: String):
        """Handle incoming head position."""
        try:
            data = json.loads(msg.data)
            self.latest_head_pitch = data.get('current_position', -10.0)
        except json.JSONDecodeError:
            pass
    
    def stream_callback(self):
        """Timer callback to stream sensor data."""
        if not self.ws_connected or self.ws is None:
            return
        
        if self.latest_image is None or self.latest_odom is None:
            # Log periodically what's missing
            if self.seq % 50 == 0:  # Every ~5 seconds at 10Hz
                missing = []
                if self.latest_image is None:
                    missing.append("camera")
                if self.latest_odom is None:
                    missing.append("odom")
                self.get_logger().warn(f"Waiting for: {', '.join(missing)}")
            self.seq += 1
            return
        
        # Build sensor data message
        odom = self.latest_odom
        odom_proto = OdomProto(
            x=odom.pose.pose.position.x,
            y=odom.pose.pose.position.y,
            yaw=quaternion_to_yaw(
                odom.pose.pose.orientation.x,
                odom.pose.pose.orientation.y,
                odom.pose.pose.orientation.z,
                odom.pose.pose.orientation.w
            ),
            timestamp=odom.header.stamp.sec + odom.header.stamp.nanosec * 1e-9
        )
        
        # Encode image to base64
        image_b64 = base64.b64encode(bytes(self.latest_image.data)).decode('utf-8')
        
        self.seq += 1
        sensor_data = SensorData(
            image_b64=image_b64,
            image_width=640,  # Assumed width - could extract from camera_info
            image_height=480,  # Assumed height
            odom=odom_proto,
            head_pitch_deg=self.latest_head_pitch,
            timestamp=time.time(),
            seq=self.seq
        )
        
        # Log first and periodic sends
        if self.seq == 1:
            self.get_logger().info(f"✓ Streaming started! First sensor data sent.")
        elif self.seq % 100 == 0:
            self.get_logger().info(f"Streamed {self.seq} frames to server")
        
        # Send via WebSocket
        asyncio.run_coroutine_threadsafe(
            self._send_message(sensor_data.to_json()),
            self.ws_loop
        )
    
    async def _send_message(self, message: str):
        """Send message via WebSocket."""
        if self.ws and self.ws_connected:
            try:
                await self.ws.send(message)
            except Exception as e:
                self.get_logger().warn(f"Failed to send message: {e}")
    
    async def handle_server_message(self, message: str):
        """Process message from inference server."""
        try:
            parsed = parse_message(message)
            
            if isinstance(parsed, WaypointCommand):
                self.get_logger().info(
                    f"Received waypoint command #{parsed.seq} with {len(parsed.waypoints)} waypoints"
                )
                
                if not self.killed and self.task_active:
                    # Get current robot pose
                    if self.latest_odom:
                        robot_x = self.latest_odom.pose.pose.position.x
                        robot_y = self.latest_odom.pose.pose.position.y
                        robot_yaw = quaternion_to_yaw(
                            self.latest_odom.pose.pose.orientation.x,
                            self.latest_odom.pose.pose.orientation.y,
                            self.latest_odom.pose.pose.orientation.z,
                            self.latest_odom.pose.pose.orientation.w
                        )
                        
                        # Update waypoints
                        self.waypoint_executor.update_waypoints(
                            parsed.waypoints, robot_x, robot_y, robot_yaw
                        )
            
            elif isinstance(parsed, TaskStart):
                self.get_logger().info(f"Task started: {parsed.task_label}")
                self.task_active = True
                self.task_label = parsed.task_label
                self.killed = False
            
            elif isinstance(parsed, TaskStop):
                self.get_logger().info(f"Task stopped: {parsed.reason}")
                self.task_active = False
                self.waypoint_executor.stop()
            
            elif isinstance(parsed, Kill):
                self.get_logger().error("!!! KILL COMMAND RECEIVED !!!")
                self.killed = True
                self.task_active = False
                self.waypoint_executor.stop()
            
            elif isinstance(parsed, Status):
                self.get_logger().info(f"Server: {parsed.message}")
            
            elif isinstance(parsed, Heartbeat):
                pass  # Heartbeat handled silently
        
        except Exception as e:
            self.get_logger().error(f"Error handling server message: {e}")
    
    async def websocket_client(self):
        """WebSocket client coroutine."""
        server_url = f"ws://{self.server_address}:{self.server_port}"
        
        while True:
            try:
                self.get_logger().info(f"Connecting to {server_url}...")
                
                async with websockets.connect(server_url, max_size=10*1024*1024) as ws:
                    self.ws = ws
                    self.ws_connected = True
                    self.get_logger().info("Connected to inference server!")
                    
                    # Send initial status
                    await ws.send(Status(
                        source="robot",
                        state="connected",
                        message="Robot client connected and ready"
                    ).to_json())
                    
                    # Message receive loop
                    async for message in ws:
                        await self.handle_server_message(message)
            
            except websockets.exceptions.ConnectionClosed:
                self.get_logger().warn("Connection to server closed")
            except Exception as e:
                self.get_logger().warn(f"WebSocket error: {e}")
            
            finally:
                self.ws = None
                self.ws_connected = False
                self.task_active = False
                self.waypoint_executor.stop()
            
            # Reconnect delay
            self.get_logger().info("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
    
    def run_websocket_client(self):
        """Run WebSocket client in background thread."""
        self.ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.ws_loop)
        self.ws_loop.run_until_complete(self.websocket_client())
    
    def destroy_node(self):
        """Clean up on shutdown."""
        self.waypoint_executor.stop()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = RobotWaypointClient()
    
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    
    try:
        executor.spin()
    except KeyboardInterrupt:
        node.get_logger().info("Shutting down...")
    finally:
        executor.shutdown()
        node.destroy_node()
        try:
            rclpy.shutdown()
        except:
            pass


if __name__ == '__main__':
    # Allow running directly with command line args
    import argparse
    parser = argparse.ArgumentParser(description="Robot Waypoint Client")
    parser.add_argument("--server_address", type=str, default="192.168.1.100",
                        help="Inference server IP address")
    parser.add_argument("--server_port", type=int, default=DEFAULT_INFERENCE_PORT,
                        help="Inference server port")
    cli_args = parser.parse_args()
    
    # Override ROS params with CLI args
    sys.argv = ['robot_client',
                '--ros-args',
                '-p', f'server_address:={cli_args.server_address}',
                '-p', f'server_port:={cli_args.server_port}']
    
    main()

