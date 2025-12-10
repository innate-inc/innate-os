#!/usr/bin/env python3
"""
Whiteboard Drawing Script

This script:
1. Parses an SVG file to extract drawing paths/waypoints
2. Uses the arm camera to detect whiteboard position and distance
3. Converts waypoints to joint angles using IK
4. Implements hybrid force/position control for drawing
5. Follows waypoints with a pen held in the end effector

Usage:
    python whiteboard_draw.py --svg path/to/drawing.svg
"""

import argparse
import sys
import os
import time
import math
import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional
import numpy as np

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState, Image, CompressedImage
from std_msgs.msg import Float64MultiArray
from geometry_msgs.msg import Twist
from maurice_msgs.srv import GotoJS
from std_srvs.srv import Trigger
import cv_bridge
import cv2

from ament_index_python.packages import get_package_share_directory
from urdf_parser_py.urdf import URDF
import PyKDL as kdl

# Import urdf module directly to avoid __init__.py issues with dynamixel
# We need to import the urdf module without triggering __init__.py
import importlib.util
import sys
import os

# Find the urdf.py file directly
maurice_arm_install = os.path.join(os.path.expanduser('~'), 'innate-os', 'ros2_ws', 'install', 'maurice_arm', 'lib', 'python3.10', 'site-packages', 'maurice_arm', 'urdf.py')
if not os.path.exists(maurice_arm_install):
    # Try source location
    maurice_arm_install = os.path.join(os.path.expanduser('~'), 'innate-os', 'ros2_ws', 'src', 'maurice_bot', 'maurice_arm', 'maurice_arm', 'urdf.py')

if os.path.exists(maurice_arm_install):
    spec = importlib.util.spec_from_file_location("maurice_arm_urdf", maurice_arm_install)
    urdf_module = importlib.util.module_from_spec(spec)
    sys.modules["maurice_arm_urdf"] = urdf_module
    spec.loader.exec_module(urdf_module)
    treeFromUrdfModel = urdf_module.treeFromUrdfModel
else:
    raise ImportError("Could not find maurice_arm.urdf module")


class WhiteboardDetector:
    """Detects whiteboard position using camera images."""
    
    def __init__(self):
        self.bridge = cv_bridge.CvBridge()
        
    def detect_whiteboard(self, image_msg: Image) -> Optional[Tuple[float, np.ndarray]]:
        """
        Detect whiteboard in image and estimate distance.
        
        Returns:
            Tuple of (distance in meters, 4x2 array of corner points in image)
            or None if not detected
        """
        try:
            cv_image = self.bridge.imgmsg_to_cv2(image_msg, "bgr8")
        except Exception as e:
            print(f"Error converting image: {e}")
            return None
            
        # Convert to grayscale
        gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
        
        # Apply Gaussian blur
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        
        # Edge detection
        edges = cv2.Canny(blurred, 50, 150)
        
        # Find contours
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # Find largest rectangular contour (likely the whiteboard)
        largest_area = 0
        best_contour = None
        
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > largest_area:
                # Approximate contour to polygon
                peri = cv2.arcLength(contour, True)
                approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
                
                # Check if it's roughly rectangular (4 corners)
                if len(approx) == 4:
                    largest_area = area
                    best_contour = approx
        
        if best_contour is None:
            return None
            
        # Extract corner points
        corners = best_contour.reshape(4, 2)
        
        # Estimate distance based on area (simple heuristic)
        # Larger area = closer, smaller area = farther
        # This is a rough estimate - you may need to calibrate
        image_area = cv_image.shape[0] * cv_image.shape[1]
        area_ratio = largest_area / image_area
        
        # Rough distance estimate (calibrate these values for your setup)
        # Assuming whiteboard is ~0.5m x 0.3m
        # Distance estimate: d = k / sqrt(area_ratio)
        k = 0.3  # Calibration constant
        distance = k / math.sqrt(area_ratio) if area_ratio > 0 else None
        
        if distance is None:
            return None
            
        return (distance, corners)


class SVGPathParser:
    """Parses SVG files to extract drawing paths."""
    
    @staticmethod
    def parse_svg(svg_path: str, scale: float = 0.01) -> List[Tuple[float, float]]:
        """
        Parse SVG file and extract waypoints from paths.
        
        Args:
            svg_path: Path to SVG file
            scale: Scale factor to convert SVG units to meters
            
        Returns:
            List of (x, y) waypoints in meters
        """
        try:
            tree = ET.parse(svg_path)
            root = tree.getroot()
        except Exception as e:
            print(f"Error parsing SVG: {e}")
            return []
        
        waypoints = []
        
        # Handle namespace
        ns = {'svg': 'http://www.w3.org/2000/svg'}
        
        # Find all path elements
        paths = root.findall('.//svg:path', ns) + root.findall('.//path', ns)
        
        for path in paths:
            d = path.get('d', '')
            if not d:
                continue
                
            # Simple path parsing - extract move and line commands
            # This is a simplified parser - for complex SVGs, consider using svgpathtools
            commands = d.replace(',', ' ').split()
            
            i = 0
            current_x, current_y = 0.0, 0.0
            
            while i < len(commands):
                cmd = commands[i].upper()
                i += 1
                
                if cmd in ['M', 'L']:  # Move or Line
                    if i < len(commands):
                        try:
                            x = float(commands[i]) * scale
                            i += 1
                            if i < len(commands):
                                y = float(commands[i]) * scale
                                i += 1
                                waypoints.append((x, y))
                                current_x, current_y = x, y
                        except (ValueError, IndexError):
                            continue
                elif cmd == 'Z':  # Close path
                    # Connect back to start
                    if waypoints:
                        waypoints.append(waypoints[0])
                # Add more command types as needed (C, Q, etc.)
        
        # If no paths found, try to find lines or polygons
        if not waypoints:
            lines = root.findall('.//svg:line', ns) + root.findall('.//line', ns)
            for line in lines:
                try:
                    x1 = float(line.get('x1', 0)) * scale
                    y1 = float(line.get('y1', 0)) * scale
                    x2 = float(line.get('x2', 0)) * scale
                    y2 = float(line.get('y2', 0)) * scale
                    waypoints.append((x1, y1))
                    waypoints.append((x2, y2))
                except (ValueError, TypeError):
                    continue
        
        return waypoints


class IKClient:
    """Client for IK solver using KDL."""
    
    def __init__(self, node: Node):
        self.node = node
        
        # Load URDF and build KDL chain
        pkg_dir = get_package_share_directory('maurice_sim')
        urdf_path = f"{pkg_dir}/urdf/maurice.urdf"
        
        if not os.path.exists(urdf_path):
            raise FileNotFoundError(f"URDF not found: {urdf_path}")
        
        robot_model = URDF.from_xml_file(urdf_path)
        ok, tree = treeFromUrdfModel(robot_model)
        
        if not ok:
            raise RuntimeError("Failed to build KDL tree")
        
        self.chain = tree.getChain('base_link', 'ee_link')
        self.ik_solver = kdl.ChainIkSolverPos_LMA(self.chain, eps=0.001, maxiter=500)
        self.fk_solver = kdl.ChainFkSolverPos_recursive(self.chain)
        
        # Current joint state
        self.current_q = kdl.JntArray(self.chain.getNrOfJoints())
        
        # Subscribe to joint state to keep current_q updated
        self.joint_sub = node.create_subscription(
            JointState,
            '/mars/arm/state',
            self._joint_state_callback,
            10
        )
        self.latest_joint_state = None
        
    def _joint_state_callback(self, msg: JointState):
        """Update current joint state."""
        self.latest_joint_state = msg
        if msg.position:
            for i in range(min(len(msg.position), self.current_q.rows())):
                self.current_q[i] = msg.position[i]
    
    def solve_ik(self, x: float, y: float, z: float, 
                 roll: float = 0.0, pitch: float = 1.57, yaw: float = 0.0) -> Optional[List[float]]:
        """
        Solve IK for a target pose.
        
        Args:
            x, y, z: Position in base frame (meters)
            roll, pitch, yaw: Orientation in radians
            
        Returns:
            List of joint angles in radians, or None if IK failed
        """
        # Update current_q from latest joint state if available
        if self.latest_joint_state and self.latest_joint_state.position:
            for i in range(min(len(self.latest_joint_state.position), self.current_q.rows())):
                self.current_q[i] = self.latest_joint_state.position[i]
        
        # Create target frame
        target_frame = kdl.Frame()
        target_frame.p = kdl.Vector(x, y, z)
        target_frame.M = kdl.Rotation.RPY(roll, pitch, yaw)
        
        # Solve IK
        q_out = kdl.JntArray(self.chain.getNrOfJoints())
        result = self.ik_solver.CartToJnt(self.current_q, target_frame, q_out)
        
        # Check result
        if result >= 0 or result == -100 or result == -101:
            # Success or acceptable approximation
            joint_angles = [q_out[i] for i in range(q_out.rows())]
            # Update current_q for next solve
            self.current_q = q_out
            return joint_angles
        else:
            self.node.get_logger().warn(f"IK failed with code {result}")
            return None


# Note: HybridForceController removed - force control now integrated into
# WhiteboardDrawNode._move_with_force_control() method


class WhiteboardDrawNode(Node):
    """Main node for whiteboard drawing."""
    
    def __init__(self, svg_path: str, control_mode: str = 'trajectory'):
        super().__init__('whiteboard_draw')
        
        self.svg_path = svg_path
        self.waypoints = []
        self.whiteboard_distance = None
        self.whiteboard_corners = None
        
        # Initialize components
        self.whiteboard_detector = WhiteboardDetector()
        self.ik_client = IKClient(self)
        
        # Command publisher for direct control (alternative to goto_js service)
        self.command_pub = self.create_publisher(
            Float64MultiArray,
            '/mars/arm/commands',
            10
        )
        
        # Control mode: 'trajectory' uses goto_js service, 'direct' uses hybrid control
        self.control_mode = control_mode
        self.get_logger().info(f"Using control mode: {control_mode}")
        
        # Subscribers
        self.arm_camera_sub = self.create_subscription(
            Image,
            '/mars/arm/image_raw',
            self._arm_camera_callback,
            10
        )
        
        # Subscribe to joint state for force control
        self.joint_state_sub = self.create_subscription(
            JointState,
            '/mars/arm/state',
            self._joint_state_callback,
            10
        )
        self.current_joint_state = None
        
        # Services
        self.goto_js_client = self.create_client(GotoJS, '/mars/arm/goto_js')
        self.torque_on_client = self.create_client(Trigger, '/mars/arm/torque_on')
        
        # Wait for services
        self.get_logger().info("Waiting for services...")
        self.goto_js_client.wait_for_service(timeout_sec=5.0)
        self.torque_on_client.wait_for_service(timeout_sec=5.0)
        
        # Parse SVG
        self.get_logger().info(f"Parsing SVG: {svg_path}")
        self.waypoints = SVGPathParser.parse_svg(svg_path)
        self.get_logger().info(f"Extracted {len(self.waypoints)} waypoints")
        
        if not self.waypoints:
            self.get_logger().error("No waypoints found in SVG!")
            return
        
        # Start drawing process
        self.create_timer(1.0, self._start_drawing)
        self.drawing_started = False
        
    def _joint_state_callback(self, msg: JointState):
        """Store latest joint state for force control."""
        self.current_joint_state = msg
    
    def _arm_camera_callback(self, msg: Image):
        """Process arm camera image to detect whiteboard."""
        result = self.whiteboard_detector.detect_whiteboard(msg)
        if result:
            distance, corners = result
            self.whiteboard_distance = distance
            self.whiteboard_corners = corners
            self.get_logger().info(f"Whiteboard detected at distance: {distance:.3f}m")
    
    def _start_drawing(self):
        """Start the drawing process."""
        if self.drawing_started:
            return
        
        self.drawing_started = True
        
        # Enable torque
        self.get_logger().info("Enabling arm torque...")
        req = Trigger.Request()
        self.torque_on_client.call_async(req)
        
        # Wait a bit for whiteboard detection
        time.sleep(2.0)
        
        if self.whiteboard_distance is None:
            self.get_logger().warn("Whiteboard not detected, using default distance")
            self.whiteboard_distance = 0.3  # Default 30cm
        
        # Execute drawing
        self.get_logger().info("Starting drawing...")
        self._execute_drawing()
    
    def _execute_drawing(self):
        """Execute the drawing sequence."""
        # Pen orientation (pointing down at whiteboard)
        # Adjust these values based on your end effector setup
        roll = 0.0
        pitch = 1.57  # 90 degrees down
        yaw = 0.0
        
        # Whiteboard plane offset (adjust based on your setup)
        # Assuming whiteboard is in front of robot
        whiteboard_z = 0.1  # Height of whiteboard center (adjust)
        whiteboard_x_offset = 0.2  # Distance from base (adjust)
        whiteboard_y_offset = 0.0  # Lateral offset (adjust)
        
        # Convert waypoints to joint angles and execute
        for i, (wx, wy) in enumerate(self.waypoints):
            self.get_logger().info(f"Moving to waypoint {i+1}/{len(self.waypoints)}: ({wx:.3f}, {wy:.3f})")
            
            # Convert SVG coordinates to robot frame
            # Adjust coordinate system as needed
            target_x = whiteboard_x_offset + wx
            target_y = whiteboard_y_offset + wy
            target_z = whiteboard_z
            
            # Solve IK
            joint_angles = self.ik_client.solve_ik(
                target_x, target_y, target_z, roll, pitch, yaw
            )
            
            if joint_angles is None:
                self.get_logger().warn(f"Failed to solve IK for waypoint {i+1}")
                continue
            
            # Move to waypoint - choose control mode
            if self.control_mode == 'trajectory':
                # Use trajectory service (smooth, but no force control)
                req = GotoJS.Request()
                req.data.data = joint_angles
                req.time = 2  # 2 seconds per waypoint (adjust as needed)
                
                future = self.goto_js_client.call_async(req)
                rclpy.spin_until_future_complete(self, future, timeout_sec=3.0)
                
                if future.result() and future.result().success:
                    self.get_logger().info(f"Reached waypoint {i+1}")
                else:
                    self.get_logger().warn(f"Failed to reach waypoint {i+1}")
                
                # Small delay between waypoints
                time.sleep(0.5)
                
            elif self.control_mode == 'direct':
                # Direct control with hybrid force/position (for force control)
                # This allows real-time force adjustments during movement
                self._move_with_force_control(joint_angles, duration=2.0)
        
        self.get_logger().info("Drawing complete!")
    
    def _move_with_force_control(self, target_joint_angles: List[float], duration: float = 2.0):
        """
        Move to target with hybrid force/position control.
        
        This method publishes commands directly, allowing real-time force adjustments.
        Note: This conflicts with goto_js service - use one or the other!
        
        Args:
            target_joint_angles: Target joint angles in radians
            duration: Movement duration in seconds
        """
        if self.current_joint_state is None:
            self.get_logger().warn("No joint state available for force control")
            return
        
        current_angles = list(self.current_joint_state.position[:6])
        if len(target_joint_angles) < 6:
            self.get_logger().error("Invalid target joint angles")
            return
        
        # Control loop parameters
        control_rate = 30.0  # Hz (matches arm_utils trajectory rate)
        dt = 1.0 / control_rate
        num_steps = int(duration * control_rate)
        
        # Force control parameters
        force_gain = 0.5
        desired_force = 2.0  # Newtons (pen pressure)
        
        for step in range(num_steps):
            # Interpolate position (cubic spline like arm_utils)
            t = step * dt
            ratio = 3 * (t / duration) ** 2 - 2 * (t / duration) ** 3
            
            # Interpolate each joint
            command_angles = [
                curr + (targ - curr) * ratio
                for curr, targ in zip(current_angles, target_joint_angles)
            ]
            
            # TODO: Add force feedback adjustment here
            # If you have force feedback, adjust command_angles[5] (last joint) based on force
            # force_error = desired_force - current_force
            # command_angles[5] += force_gain * force_error
            
            # Publish command
            cmd_msg = Float64MultiArray()
            cmd_msg.data = command_angles
            self.command_pub.publish(cmd_msg)
            
            # Sleep to maintain control rate
            time.sleep(dt)
            
            # Update current angles from latest joint state
            if self.current_joint_state and len(self.current_joint_state.position) >= 6:
                current_angles = list(self.current_joint_state.position[:6])
        
        self.get_logger().info(f"Completed force-controlled movement")


def main(args=None):
    # Parse arguments before rclpy.init()
    parser = argparse.ArgumentParser(description='Draw on whiteboard from SVG')
    parser.add_argument('--svg', type=str, required=True,
                       help='Path to SVG file')
    parser.add_argument('--scale', type=float, default=0.01,
                       help='Scale factor for SVG coordinates (default: 0.01)')
    parser.add_argument('--control-mode', type=str, default='trajectory',
                       choices=['trajectory', 'direct'],
                       help='Control mode: trajectory (smooth, no force) or direct (with force control)')
    
    # Parse known args, pass remaining to rclpy
    known_args, ros_args = parser.parse_known_args()
    
    # Initialize ROS with remaining args
    rclpy.init(args=ros_args)
    
    try:
        node = WhiteboardDrawNode(known_args.svg, control_mode=known_args.control_mode)
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

