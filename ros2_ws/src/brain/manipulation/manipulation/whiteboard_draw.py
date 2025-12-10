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
    ros2 run manipulation whiteboard_draw --svg path/to/drawing.svg --control-mode direct
"""

import argparse
import sys
import os
import time
import math
import json
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
import importlib.util

# Find the urdf.py file directly
maurice_arm_src = os.path.join(os.path.expanduser('~'), 'innate-os', 'ros2_ws', 'src', 'maurice_bot', 'maurice_arm', 'maurice_arm', 'urdf.py')
if os.path.exists(maurice_arm_src):
    spec = importlib.util.spec_from_file_location("maurice_arm_urdf", maurice_arm_src)
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
    def parse_svg(svg_path: str) -> List[Tuple[float, float]]:
        """
        Parse SVG file and extract waypoints from paths.
        Returns waypoints in SVG coordinates (relative to viewBox/page).
        
        Args:
            svg_path: Path to SVG file
            
        Returns:
            List of (x, y) waypoints in SVG coordinates
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
                            x = float(commands[i])  # Keep in SVG coordinates
                            i += 1
                            if i < len(commands):
                                y = float(commands[i])  # Keep in SVG coordinates
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
                    x1 = float(line.get('x1', 0))  # Keep in SVG coordinates
                    y1 = float(line.get('y1', 0))
                    x2 = float(line.get('x2', 0))
                    y2 = float(line.get('y2', 0))
                    waypoints.append((x1, y1))
                    waypoints.append((x2, y2))
                except (ValueError, TypeError):
                    continue
            
            # Also check for rectangles
            rects = root.findall('.//svg:rect', ns) + root.findall('.//rect', ns)
            for rect in rects:
                try:
                    x = float(rect.get('x', 0))  # Keep in SVG coordinates
                    y = float(rect.get('y', 0))
                    width = float(rect.get('width', 0))
                    height = float(rect.get('height', 0))
                    # Create rectangle waypoints
                    waypoints.append((x, y))
                    waypoints.append((x + width, y))
                    waypoints.append((x + width, y + height))
                    waypoints.append((x, y + height))
                    waypoints.append((x, y))  # Close rectangle
                except (ValueError, TypeError):
                    continue
        
        return waypoints


class IKClient:
    """Client for IK solver using KDL."""
    
    # Joint limits from arm_config.yaml (in radians)
    JOINT_LIMITS = [
        (-1.5708, 1.5708),   # joint_1
        (-1.5708, 1.22),     # joint_2
        (-1.5708, 1.7453),   # joint_3
        (-1.9199, 1.7453),   # joint_4
        (-1.5708, 1.5708),   # joint_5
        (-0.8727, 0.3491),   # joint_6
        (-0.4363, 0.2618),   # joint_7
    ]
    
    def __init__(self, node: Node, pen_tip_offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)):
        """
        Initialize IK client.
        
        Args:
            node: ROS node
            pen_tip_offset: (x, y, z) offset from ee_link to pen tip in end effector frame (meters).
                           Positive z is typically down (along the end effector's negative z axis when pointing down).
                           This offset is applied to transform target positions from pen tip to ee_link.
        """
        self.node = node
        self.pen_tip_offset = pen_tip_offset
        
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
        
        if any(abs(v) > 0.001 for v in pen_tip_offset):
            self.node.get_logger().info(
                f"Pen tip offset (in ee frame): ({pen_tip_offset[0]:.4f}, {pen_tip_offset[1]:.4f}, {pen_tip_offset[2]:.4f}) m"
            )
        
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
    
    def _validate_joint_limits(self, joint_angles: List[float]) -> Tuple[bool, Optional[str]]:
        """Validate joint angles are within limits."""
        for i, angle in enumerate(joint_angles):
            if i >= len(self.JOINT_LIMITS):
                continue  # Skip if no limit defined
            min_limit, max_limit = self.JOINT_LIMITS[i]
            if angle < min_limit or angle > max_limit:
                return False, f"Joint {i+1} angle {angle:.3f} outside limits [{min_limit:.3f}, {max_limit:.3f}]"
        return True, None
    
    def _verify_fk(self, joint_angles: List[float], target_pos: Tuple[float, float, float], 
                   target_rot: Tuple[float, float, float], pos_tolerance: float = 0.01, 
                   rot_tolerance: float = 1.5) -> Tuple[bool, str]:
        """Verify IK solution using forward kinematics.
        
        Note: joint_angles should match the chain's joint count (not padded).
        """
        # Use only the number of joints in the chain (not padded)
        num_joints = min(len(joint_angles), self.chain.getNrOfJoints())
        
        # Convert to KDL joint array (only chain joints)
        q_test = kdl.JntArray(num_joints)
        for i in range(num_joints):
            q_test[i] = joint_angles[i]
        
        # Compute FK
        result_frame = kdl.Frame()
        fk_result = self.fk_solver.JntToCart(q_test, result_frame)
        
        if fk_result < 0:
            return False, f"FK calculation failed with code {fk_result}"
        
        # Check position error
        pos_error = (
            (result_frame.p.x() - target_pos[0])**2 +
            (result_frame.p.y() - target_pos[1])**2 +
            (result_frame.p.z() - target_pos[2])**2
        ) ** 0.5
        
        if pos_error > pos_tolerance:
            return False, f"Position error {pos_error:.4f}m exceeds tolerance {pos_tolerance:.4f}m"
        
        # Check orientation error (simplified - just check RPY)
        result_rpy = result_frame.M.GetRPY()
        rot_error = (
            (result_rpy[0] - target_rot[0])**2 +
            (result_rpy[1] - target_rot[1])**2 +
            (result_rpy[2] - target_rot[2])**2
        ) ** 0.5
        
        if rot_error > rot_tolerance:
            return False, f"Orientation error {rot_error:.4f}rad exceeds tolerance {rot_tolerance:.4f}rad"
        
        return True, f"FK verified: pos_error={pos_error:.4f}m, rot_error={rot_error:.4f}rad"
    
    def _solve_ik_single_orientation(self, x: float, y: float, z: float, 
                                       roll: float, pitch: float, yaw: float) -> Optional[Tuple[List[float], int]]:
        """Try to solve IK for a single orientation. Returns (joint_angles, result_code) or None."""
        # Create target frame
        target_frame = kdl.Frame()
        target_frame.p = kdl.Vector(x, y, z)
        target_frame.M = kdl.Rotation.RPY(roll, pitch, yaw)
        
        q_out = kdl.JntArray(self.chain.getNrOfJoints())
        
        # Try solving with current seed
        result = self.ik_solver.CartToJnt(self.current_q, target_frame, q_out)
        
        # If failed, try with zero seed
        if result < 0 and result != -100 and result != -101:
            zero_q = kdl.JntArray(self.chain.getNrOfJoints())
            result = self.ik_solver.CartToJnt(zero_q, target_frame, q_out)
        
        # If still failed, try with random seeds
        if result < 0 and result != -100 and result != -101:
            import random
            for attempt in range(3):
                random_q = kdl.JntArray(self.chain.getNrOfJoints())
                for i in range(random_q.rows()):
                    if i < len(self.JOINT_LIMITS):
                        min_limit, max_limit = self.JOINT_LIMITS[i]
                        random_q[i] = random.uniform(min_limit * 0.8, max_limit * 0.8)
                    else:
                        random_q[i] = random.uniform(-1.0, 1.0)
                result = self.ik_solver.CartToJnt(random_q, target_frame, q_out)
                if result >= 0 or result == -100 or result == -101:
                    break
        
        if result >= 0 or result == -100 or result == -101:
            joint_angles = [q_out[i] for i in range(q_out.rows())]
            return (joint_angles, result)
        return None
    
    def solve_ik(self, x: float, y: float, z: float, 
                 roll: float = 0.0, pitch: float = 1.57, yaw: float = 0.0) -> Optional[List[float]]:
        """
        Solve IK for a target pose with validation, trying multiple orientations if needed.
        
        Args:
            x, y, z: Position of PEN TIP in base frame (meters)
            roll, pitch, yaw: Preferred orientation in radians
            
        Returns:
            List of joint angles in radians, or None if IK failed or solution invalid
        """
        # Update current_q from latest joint state if available
        if self.latest_joint_state and self.latest_joint_state.position:
            for i in range(min(len(self.latest_joint_state.position), self.current_q.rows())):
                self.current_q[i] = self.latest_joint_state.position[i]
        
        # Log current pose for debugging
        current_frame = kdl.Frame()
        fk_result = self.fk_solver.JntToCart(self.current_q, current_frame)
        if fk_result >= 0:
            self.node.get_logger().debug(
                f"Current ee_link pose: ({current_frame.p.x():.3f}, {current_frame.p.y():.3f}, {current_frame.p.z():.3f})"
            )
        
        # Transform pen tip position to ee_link position
        # The target (x, y, z) is the pen tip position
        # We need to compute where ee_link should be to place pen tip at (x, y, z)
        # pen_tip = ee_link + R * offset (where R is the end effector rotation)
        # So: ee_link = pen_tip - R * offset
        
        # Create rotation matrix for target orientation
        target_rot = kdl.Rotation.RPY(roll, pitch, yaw)
        
        # Transform offset from end effector frame to base frame
        offset_in_base = target_rot * kdl.Vector(
            self.pen_tip_offset[0],
            self.pen_tip_offset[1],
            self.pen_tip_offset[2]
        )
        
        # Compute ee_link target position
        ee_x = x - offset_in_base.x()
        ee_y = y - offset_in_base.y()
        ee_z = z - offset_in_base.z()
        
        self.node.get_logger().info(
            f"IK target (pen tip): pos=({x:.3f}, {y:.3f}, {z:.3f}), preferred rpy=({roll:.3f}, {pitch:.3f}, {yaw:.3f})"
        )
        if any(abs(v) > 0.001 for v in self.pen_tip_offset):
            self.node.get_logger().info(
                f"  -> ee_link target: pos=({ee_x:.3f}, {ee_y:.3f}, {ee_z:.3f})"
            )
        
        # Try multiple orientations if the preferred one fails
        # For drawing on ground/paper, try different pitch angles
        orientations_to_try = [
            (roll, pitch, yaw),  # Preferred orientation
            (roll, pitch * 0.75, yaw),  # Less steep
            (roll, pitch * 0.5, yaw),  # Even less steep
            (roll, pitch * 0.25, yaw),  # Almost horizontal
            (roll, 0.0, yaw),  # Horizontal
            (roll, -pitch * 0.5, yaw),  # Slightly upward
        ]
        
        best_solution = None
        best_error = float('inf')
        
        for r, p, y in orientations_to_try:
            # For each orientation, compute the corresponding ee_link position
            rot = kdl.Rotation.RPY(r, p, y)
            offset_in_base = rot * kdl.Vector(
                self.pen_tip_offset[0],
                self.pen_tip_offset[1],
                self.pen_tip_offset[2]
            )
            ee_x_orient = x - offset_in_base.x()
            ee_y_orient = y - offset_in_base.y()
            ee_z_orient = z - offset_in_base.z()
            
            result = self._solve_ik_single_orientation(ee_x_orient, ee_y_orient, ee_z_orient, r, p, y)
            if result is None:
                continue
            
            joint_angles, result_code = result
            
            # Validate joint limits
            valid_limits, limit_error = self._validate_joint_limits(joint_angles)
            if not valid_limits:
                self.node.get_logger().debug(
                    f"Orientation rpy=({r:.3f}, {p:.3f}, {y:.3f}) violates joint limits: {limit_error}"
                )
                continue
            
            # Verify FK - check pen tip position (not ee_link position)
            # Compute expected pen tip position from ee_link FK result
            num_joints = min(len(joint_angles), self.chain.getNrOfJoints())
            q_test = kdl.JntArray(num_joints)
            for i in range(num_joints):
                q_test[i] = joint_angles[i]
            ee_frame = kdl.Frame()
            self.fk_solver.JntToCart(q_test, ee_frame)
            
            # Transform pen tip offset to base frame
            rot = kdl.Rotation.RPY(r, p, y)
            offset_in_base = rot * kdl.Vector(
                self.pen_tip_offset[0],
                self.pen_tip_offset[1],
                self.pen_tip_offset[2]
            )
            pen_tip_pos = (
                ee_frame.p.x() + offset_in_base.x(),
                ee_frame.p.y() + offset_in_base.y(),
                ee_frame.p.z() + offset_in_base.z()
            )
            
            # Verify pen tip position matches target
            target_pos = (x, y, z)  # This is the pen tip target
            target_rot = (r, p, y)
            pos_error = (
                (pen_tip_pos[0] - target_pos[0])**2 +
                (pen_tip_pos[1] - target_pos[1])**2 +
                (pen_tip_pos[2] - target_pos[2])**2
            ) ** 0.5
            
            if pos_error > 0.02:
                self.node.get_logger().debug(
                    f"Orientation rpy=({r:.3f}, {p:.3f}, {y:.3f}) pen tip position error {pos_error:.4f}m exceeds tolerance"
                )
                continue
            
            # Check orientation error
            result_rpy = ee_frame.M.GetRPY()
            rot_error = (
                (result_rpy[0] - target_rot[0])**2 +
                (result_rpy[1] - target_rot[1])**2 +
                (result_rpy[2] - target_rot[2])**2
            ) ** 0.5
            
            if rot_error > 1.5:  # orientation tolerance
                self.node.get_logger().debug(
                    f"Orientation rpy=({r:.3f}, {p:.3f}, {y:.3f}) orientation error {rot_error:.4f}rad exceeds tolerance"
                )
                continue
            
            fk_msg = f"FK verified: pen_tip_pos_error={pos_error:.4f}m, rot_error={rot_error:.4f}rad"
            
            # All validations passed - use this solution
            # pos_error is already calculated above for pen tip position
            
            if pos_error < best_error:
                best_error = pos_error
                best_solution = (joint_angles, r, p, y, fk_msg)
        
        if best_solution is None:
            self.node.get_logger().warn(
                f"IK failed for all orientations at target ({x:.3f}, {y:.3f}, {z:.3f})"
            )
            return None
        
        joint_angles, final_roll, final_pitch, final_yaw, fk_msg = best_solution
        
        self.node.get_logger().info(
            f"IK solved with orientation rpy=({final_roll:.3f}, {final_pitch:.3f}, {final_yaw:.3f})"
        )
        
        # Pad to 6 joints if needed (arm expects 6 joints)
        if len(joint_angles) < 6:
            self.node.get_logger().debug(f"IK returned {len(joint_angles)} joints, padding to 6 with zeros")
            joint_angles.extend([0.0] * (6 - len(joint_angles)))
        elif len(joint_angles) > 6:
            self.node.get_logger().warn(f"IK returned {len(joint_angles)} joints, truncating to 6")
            joint_angles = joint_angles[:6]
        
            # Update current_q for next solve
        q_out = kdl.JntArray(len(joint_angles[:self.chain.getNrOfJoints()]))
        for i in range(min(len(joint_angles), self.chain.getNrOfJoints())):
            q_out[i] = joint_angles[i]
            self.current_q = q_out
        
        self.node.get_logger().info(f"  {fk_msg}")
        self.node.get_logger().debug(f"  Joint angles: {[f'{a:.3f}' for a in joint_angles[:self.chain.getNrOfJoints()]]}")
        
            return joint_angles


class WhiteboardCalibrator:
    """Calibrates whiteboard coordinate system from corner positions."""
    
    def __init__(self, node: Node, ik_client: IKClient):
        self.node = node
        self.ik_client = ik_client
        self.corners = None  # List of 4 (x, y, z) tuples in robot frame
        self.svg_viewbox = (100.0, 100.0)  # Default SVG viewBox dimensions
        
    def set_corners(self, corners: List[Tuple[float, float, float]]):
        """
        Set the four corners of the whiteboard in robot frame.
        
        Args:
            corners: List of 4 tuples [(x1,y1,z1), (x2,y2,z2), (x3,y3,z3), (x4,y4,z4)]
                    Order: top-left, top-right, bottom-right, bottom-left (in SVG space)
        """
        if len(corners) != 4:
            raise ValueError("Must provide exactly 4 corners")
        self.corners = corners
        self.node.get_logger().info("Whiteboard corners calibrated:")
        for i, (x, y, z) in enumerate(corners):
            self.node.get_logger().info(f"  Corner {i+1}: ({x:.3f}, {y:.3f}, {z:.3f})")
    
    def set_svg_viewbox(self, width: float, height: float):
        """Set SVG viewBox dimensions for coordinate mapping."""
        self.svg_viewbox = (width, height)
    
    def svg_to_robot(self, svg_x: float, svg_y: float) -> Tuple[float, float, float]:
        """
        Convert SVG coordinates to robot frame coordinates using bilinear interpolation.
        
        Args:
            svg_x, svg_y: SVG coordinates (in viewBox units)
            
        Returns:
            (x, y, z) in robot frame
        """
        if self.corners is None:
            raise RuntimeError("Whiteboard not calibrated - call calibrate() first")
        
        # Normalize SVG coordinates to 0-1 range
        u = svg_x / self.svg_viewbox[0]
        v = svg_y / self.svg_viewbox[1]
        
        # Bilinear interpolation to map from SVG (u, v) to robot (x, y, z)
        # Corners order: top-left (0,0), top-right (1,0), bottom-right (1,1), bottom-left (0,1)
        tl = self.corners[0]  # top-left
        tr = self.corners[1]  # top-right
        br = self.corners[2]  # bottom-right
        bl = self.corners[3]  # bottom-left
        
        # Interpolate top edge
        top_x = tl[0] * (1 - u) + tr[0] * u
        top_y = tl[1] * (1 - u) + tr[1] * u
        top_z = tl[2] * (1 - u) + tr[2] * u
        
        # Interpolate bottom edge
        bot_x = bl[0] * (1 - u) + br[0] * u
        bot_y = bl[1] * (1 - u) + br[1] * u
        bot_z = bl[2] * (1 - u) + br[2] * u
        
        # Interpolate vertically
        x = top_x * (1 - v) + bot_x * v
        y = top_y * (1 - v) + bot_y * v
        z = top_z * (1 - v) + bot_z * v
        
        return (x, y, z)


class WhiteboardDrawNode(Node):
    """Main node for whiteboard drawing."""
    
    def __init__(self, svg_path: str, control_mode: str = 'trajectory', skip_calibration: bool = False, 
                 calibration_file: Optional[str] = None, pen_tip_offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)):
        super().__init__('whiteboard_draw')
        
        self.svg_path = svg_path
        self.waypoints = []
        self.whiteboard_distance = None
        self.whiteboard_corners = None
        self.skip_calibration = skip_calibration
        self.calibration_file = calibration_file
        
        # Initialize components
        self.whiteboard_detector = WhiteboardDetector()
        self.ik_client = IKClient(self, pen_tip_offset=pen_tip_offset)
        self.calibrator = WhiteboardCalibrator(self, self.ik_client)
        
        # Command publisher for direct control (alternative to goto_js service)
        self.command_pub = self.create_publisher(
            Float64MultiArray,
            '/mars/arm/commands',
            10
        )
        
        # Control mode: 'trajectory' uses goto_js service, 'direct' uses hybrid control
        self.control_mode = control_mode
        self.get_logger().info(f"Using control mode: {control_mode}")
        
        # Subscribers with matching QoS (sensor data, best effort)
        # Note: Camera subscription is created but will be inactive during calibration
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
        
        sensor_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        
        # Create camera subscription but it will be quiet during calibration
        self.arm_camera_sub = self.create_subscription(
            Image,
            '/mars/arm/image_raw',
            self._arm_camera_callback,
            sensor_qos
        )
        self.get_logger().info("Camera subscription created (will be active after calibration)")
        
        # Subscribe to joint state for force control
        self.joint_state_sub = self.create_subscription(
            JointState,
            '/mars/arm/state',
            self._joint_state_callback,
            10
        )
        self.current_joint_state = None
        
        # Services (clients)
        self.goto_js_client = self.create_client(GotoJS, '/mars/arm/goto_js')
        self.torque_on_client = self.create_client(Trigger, '/mars/arm/torque_on')
        
        # Services (servers) for calibration interaction
        from std_srvs.srv import Trigger as TriggerSrv
        self.record_corner_service = self.create_service(
            TriggerSrv,
            '/whiteboard_draw/record_corner',
            self._record_corner_callback
        )
        self.start_drawing_service = self.create_service(
            TriggerSrv,
            '/whiteboard_draw/start_drawing',
            self._start_drawing_callback
        )
        
        # Wait for services
        self.get_logger().info("Waiting for services...")
        self.goto_js_client.wait_for_service(timeout_sec=5.0)
        self.torque_on_client.wait_for_service(timeout_sec=5.0)
        
        self.get_logger().info("Calibration services available:")
        self.get_logger().info("  - /whiteboard_draw/record_corner (call to record current corner)")
        self.get_logger().info("  - /whiteboard_draw/start_drawing (call to start drawing after calibration)")
        
        # Parse SVG - resolve path if relative
        self.svg_path_abs = svg_path
        if not os.path.isabs(svg_path):
            # Try relative to current directory first
            if os.path.exists(svg_path):
                self.svg_path_abs = os.path.abspath(svg_path)
            else:
                # Try relative to innate-os root
                innate_os_root = os.environ.get('INNATE_OS_ROOT', os.path.expanduser('~/innate-os'))
                potential_path = os.path.join(innate_os_root, svg_path)
                if os.path.exists(potential_path):
                    self.svg_path_abs = potential_path
                else:
                    # Try just the filename in innate-os root
                    filename = os.path.basename(svg_path)
                    potential_path = os.path.join(innate_os_root, filename)
                    if os.path.exists(potential_path):
                        self.svg_path_abs = potential_path
                    else:
                        self.get_logger().error(f"SVG file not found: {svg_path}")
                        self.get_logger().error(f"  Tried: {os.path.abspath(svg_path)}")
                        self.get_logger().error(f"  Tried: {os.path.join(innate_os_root, svg_path)}")
                        self.get_logger().error(f"  Tried: {potential_path}")
                        return
        
        if not os.path.exists(self.svg_path_abs):
            self.get_logger().error(f"SVG file does not exist: {self.svg_path_abs}")
            return
        
        self.get_logger().info(f"Parsing SVG: {self.svg_path_abs}")
        self.waypoints = SVGPathParser.parse_svg(self.svg_path_abs)
        self.get_logger().info(f"Extracted {len(self.waypoints)} waypoints (in SVG coordinates)")
        
        if not self.waypoints:
            self.get_logger().error("No waypoints found in SVG!")
            return
        
        # Get SVG viewBox for calibration
        self._parse_svg_viewbox(self.svg_path_abs)
        
        # Start calibration or drawing process
        self.skip_calibration = skip_calibration
        self.calibration_state = 'waiting'
        self.calibration_corners = []
        self.calibration_complete = False
        
        # Try to load calibration from file
        if self.calibration_file and os.path.exists(self.calibration_file):
            if self._load_calibration(self.calibration_file):
                self.get_logger().info(f"✓ Loaded calibration from {self.calibration_file}")
                self.calibration_complete = True
            else:
                self.get_logger().warn(f"Failed to load calibration from {self.calibration_file}, will need to recalibrate")
        
        if not self.calibration_complete and not skip_calibration:
            self.get_logger().info("=" * 60)
            self.get_logger().info("CALIBRATION MODE")
            self.get_logger().info("=" * 60)
            self.get_logger().info("Move the arm to each corner and call the service:")
            self.get_logger().info("  ros2 service call /whiteboard_draw/record_corner std_srvs/srv/Trigger")
            self.get_logger().info("Corner order: top-left, top-right, bottom-right, bottom-left")
            self.get_logger().info("=" * 60)
            self.get_logger().info("After all 4 corners, call:")
            self.get_logger().info("  ros2 service call /whiteboard_draw/start_drawing std_srvs/srv/Trigger")
            if self.calibration_file:
                self.get_logger().info(f"Calibration will be saved to: {self.calibration_file}")
            self.get_logger().info("=" * 60)
        elif skip_calibration:
            self.get_logger().info("Skipping calibration - using default mapping")
            self.calibration_complete = True
        
        self.drawing_started = False
    
    def _save_calibration(self, filepath: str) -> bool:
        """Save calibration corners to a JSON file."""
        try:
            calibration_data = {
                'corners': self.calibration_corners,
                'svg_viewbox': list(self.calibrator.svg_viewbox) if self.calibrator.svg_viewbox else None,
                'timestamp': time.time()
            }
            
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)
            
            with open(filepath, 'w') as f:
                json.dump(calibration_data, f, indent=2)
            
            self.get_logger().info(f"Saved calibration to {filepath}")
            return True
        except Exception as e:
            self.get_logger().error(f"Failed to save calibration to {filepath}: {e}")
            return False
    
    def _load_calibration(self, filepath: str) -> bool:
        """Load calibration corners from a JSON file."""
        try:
            with open(filepath, 'r') as f:
                calibration_data = json.load(f)
            
            corners = calibration_data.get('corners', [])
            if len(corners) != 4:
                self.get_logger().error(f"Invalid calibration file: expected 4 corners, got {len(corners)}")
                return False
            
            # Convert to tuples
            self.calibration_corners = [tuple(c) for c in corners]
            
            # Set SVG viewbox if available
            svg_viewbox = calibration_data.get('svg_viewbox')
            if svg_viewbox:
                self.calibrator.set_svg_viewbox(svg_viewbox[0], svg_viewbox[1])
            
            # Set corners in calibrator
            self.calibrator.set_corners(self.calibration_corners)
            
            self.get_logger().info(f"Loaded calibration corners:")
            corner_names = ['top-left', 'top-right', 'bottom-right', 'bottom-left']
            for i, (name, corner) in enumerate(zip(corner_names, self.calibration_corners)):
                self.get_logger().info(f"  {name}: ({corner[0]:.3f}, {corner[1]:.3f}, {corner[2]:.3f})")
            
            return True
        except Exception as e:
            self.get_logger().error(f"Failed to load calibration from {filepath}: {e}")
            return False
    
    def _parse_svg_viewbox(self, svg_path: str):
        """Parse SVG viewBox dimensions."""
        try:
            tree = ET.parse(svg_path)
            root = tree.getroot()
            viewbox = root.get('viewBox', '')
            if viewbox:
                parts = viewbox.split()
                if len(parts) >= 4:
                    width = float(parts[2])
                    height = float(parts[3])
                    self.calibrator.set_svg_viewbox(width, height)
                    self.get_logger().info(f"SVG viewBox: {width} x {height}")
        except Exception as e:
            self.get_logger().warn(f"Could not parse SVG viewBox: {e}")
    
    def _record_corner_callback(self, request, response):
        """Service callback to record current corner position."""
        if self.calibration_complete:
            response.success = False
            response.message = "Calibration already complete"
            return response
        
        corner_names = ['top-left', 'top-right', 'bottom-right', 'bottom-left']
        corner_idx = len(self.calibration_corners)
        
        if corner_idx >= 4:
            response.success = False
            response.message = "All 4 corners already recorded"
            return response
        
        # Get current end effector position using FK
        if not self.ik_client.latest_joint_state or not self.ik_client.latest_joint_state.position:
            response.success = False
            response.message = "No joint state available - make sure arm is powered and reporting state"
            return response
        
        current_frame = kdl.Frame()
        # Update current_q
        for i in range(min(len(self.ik_client.latest_joint_state.position), self.ik_client.current_q.rows())):
            self.ik_client.current_q[i] = self.ik_client.latest_joint_state.position[i]
        
        fk_result = self.ik_client.fk_solver.JntToCart(self.ik_client.current_q, current_frame)
        
        if fk_result >= 0:
            x = current_frame.p.x()
            y = current_frame.p.y()
            z = current_frame.p.z()
            
            self.calibration_corners.append((x, y, z))
            self.get_logger().info(
                f"✓ Recorded {corner_names[corner_idx]}: ({x:.3f}, {y:.3f}, {z:.3f})"
            )
            
            if corner_idx < 3:
                response.success = True
                response.message = f"Recorded {corner_names[corner_idx]}. Move to {corner_names[corner_idx + 1]} and call service again."
            else:
                # All corners recorded
                self.calibrator.set_corners(self.calibration_corners)
                self.calibration_complete = True
                
                # Save calibration to file if specified
                if self.calibration_file:
                    self._save_calibration(self.calibration_file)
                
                self.get_logger().info("=" * 60)
                self.get_logger().info("✓ CALIBRATION COMPLETE!")
                self.get_logger().info("=" * 60)
                self.get_logger().info("Call /whiteboard_draw/start_drawing service to begin")
                response.success = True
                response.message = "All 4 corners recorded! Calibration complete. Call /whiteboard_draw/start_drawing to begin."
        else:
            response.success = False
            response.message = "Failed to compute FK - make sure arm is in a valid position"
        
        return response
    
    def _start_drawing_callback(self, request, response):
        """Service callback to start drawing."""
        if not self.calibration_complete and not self.skip_calibration:
            response.success = False
            response.message = "Calibration not complete. Record all 4 corners first."
            return response
        
        if self.drawing_started:
            response.success = False
            response.message = "Drawing already started"
            return response
        
        self.get_logger().info("Starting drawing sequence via service call...")
        self._start_drawing()
        response.success = True
        response.message = "Drawing sequence started"
        return response
    
    def _calibration_loop(self):
        """Timer callback - no longer needed but kept for compatibility."""
        # Calibration now handled via service calls
        pass
        
    def _joint_state_callback(self, msg: JointState):
        """Store latest joint state for force control."""
        self.current_joint_state = msg
    
    def _arm_camera_callback(self, msg: Image):
        """Process arm camera image to detect whiteboard."""
        # Skip detection during calibration - not needed
        if not self.calibration_complete and not self.skip_calibration:
            return
        
        # Skip if drawing hasn't started yet (camera not needed until drawing)
        if not self.drawing_started:
            return
        
        # For now, disable camera detection entirely - not needed for basic drawing
        # Can be re-enabled later if needed for adaptive drawing
        return
        
        # Uncomment below if you want camera-based whiteboard detection during drawing
        # result = self.whiteboard_detector.detect_whiteboard(msg)
        # if result:
        #     distance, corners = result
        #     # Only log if distance changed significantly (avoid spam)
        #     if self.whiteboard_distance is None or abs(distance - self.whiteboard_distance) > 0.5:
        #         self.get_logger().info(f"Whiteboard detected at distance: {distance:.3f}m")
        #     self.whiteboard_distance = distance
        #     self.whiteboard_corners = corners
    
    def _start_drawing(self):
        """Start the drawing process."""
        if self.drawing_started:
            self.get_logger().warn("Drawing already started, ignoring request")
            return
        
        self.drawing_started = True
        
        # Enable torque
        self.get_logger().info("Enabling arm torque...")
        req = Trigger.Request()
        future = self.torque_on_client.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)
        if future.result():
            self.get_logger().info(f"Torque enabled: {future.result().message}")
        time.sleep(1.0)
        
        self.get_logger().info("=" * 60)
        self.get_logger().info("STARTING DRAWING SEQUENCE")
        self.get_logger().info("=" * 60)
        
        # Execute drawing in a separate thread to avoid blocking the service callback
        import threading
        drawing_thread = threading.Thread(target=self._execute_drawing, daemon=True)
        drawing_thread.start()
    
    def _execute_drawing(self):
        """Execute the drawing sequence."""
        self.get_logger().info("_execute_drawing() called - starting waypoint execution")
        
        if not self.waypoints:
            self.get_logger().error("No waypoints to execute!")
            return
        
        # Pen orientation (pointing down at whiteboard)
        # Adjust these values based on your end effector setup
        roll = 0.0
        pitch = 1.57  # 90 degrees down
        yaw = 0.0
        
        self.get_logger().info(f"Executing {len(self.waypoints)} waypoints...")
        
        # Convert waypoints to joint angles and execute
        for i, (wx, wy) in enumerate(self.waypoints):
            self.get_logger().info(f"Moving to waypoint {i+1}/{len(self.waypoints)}: SVG=({wx:.3f}, {wy:.3f})")
            
            # Convert SVG coordinates to robot frame
            if self.calibrator.corners is not None:
                # Use calibrated mapping
                target_x, target_y, target_z = self.calibrator.svg_to_robot(wx, wy)
                self.get_logger().info(f"  Calibrated robot target: ({target_x:.3f}, {target_y:.3f}, {target_z:.3f})")
            else:
                # Fallback to default mapping (if calibration was skipped)
                whiteboard_z = 0.15
                whiteboard_x_offset = 0.25
                whiteboard_y_offset = 0.0
            target_x = whiteboard_x_offset + wx
                target_y = whiteboard_y_offset - wy
            target_z = whiteboard_z
                self.get_logger().info(f"  Default robot target: ({target_x:.3f}, {target_y:.3f}, {target_z:.3f})")
            
            # Solve IK
            joint_angles = self.ik_client.solve_ik(
                target_x, target_y, target_z, roll, pitch, yaw
            )
            
            if joint_angles is None:
                self.get_logger().error(
                    f"✗ Waypoint {i+1}/{len(self.waypoints)} UNREACHABLE: "
                    f"target=({target_x:.3f}, {target_y:.3f}, {target_z:.3f})"
                )
                self.get_logger().error(
                    f"  Skipping waypoint {i+1} - IK failed or solution invalid "
                    f"(outside joint limits or FK verification failed)"
                )
                continue
            
            # Move to waypoint - choose control mode
            if self.control_mode == 'trajectory':
                # Use trajectory service (smooth, but no force control)
                req = GotoJS.Request()
                req.data.data = joint_angles
                req.time = 2  # 2 seconds per waypoint (adjust as needed)
                
                future = self.goto_js_client.call_async(req)
                # Wait for service response (check periodically since we're in a thread)
                start_time = time.time()
                timeout = 5.0
                while not future.done() and (time.time() - start_time) < timeout:
                    time.sleep(0.1)
                
                if future.done():
                    try:
                        result = future.result()
                        if result and result.success:
                            self.get_logger().info(f"✓ Reached waypoint {i+1}")
                else:
                            self.get_logger().warn(f"✗ Failed to reach waypoint {i+1}")
                    except Exception as e:
                        self.get_logger().error(f"Error getting waypoint {i+1} result: {e}")
                else:
                    self.get_logger().warn(f"Waypoint {i+1} service call timed out")
                
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
    parser.add_argument('--skip-calibration', action='store_true',
                       help='Skip calibration and use default coordinate mapping')
    parser.add_argument('--calibration-file', type=str, default=None,
                       help='Path to JSON file to save/load calibration corners')
    parser.add_argument('--pen-tip-offset', type=str, default='0.0,0.0,0.0',
                       help='Pen tip offset from ee_link in end effector frame (x,y,z in meters). '
                            'For pen pointing down, typically (0, 0, -pen_length). Default: 0,0,0')
    
    # Parse known args, pass remaining to rclpy
    known_args, ros_args = parser.parse_known_args()
    
    # Parse pen tip offset
    try:
        offset_parts = [float(x.strip()) for x in known_args.pen_tip_offset.split(',')]
        if len(offset_parts) != 3:
            raise ValueError("Pen tip offset must have 3 values")
        pen_tip_offset = tuple(offset_parts)
    except Exception as e:
        print(f"Error parsing pen-tip-offset: {e}. Using default (0,0,0)")
        pen_tip_offset = (0.0, 0.0, 0.0)
    
    # Initialize ROS with remaining args
    rclpy.init(args=ros_args)
    
    try:
        node = WhiteboardDrawNode(
            known_args.svg, 
            control_mode=known_args.control_mode,
            skip_calibration=known_args.skip_calibration,
            calibration_file=known_args.calibration_file,
            pen_tip_offset=pen_tip_offset
        )
        
        # No input thread needed - use ROS services instead
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
