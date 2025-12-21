#!/usr/bin/env python3
"""
Vision Obstacle Detector Node

Detects objects from camera images and publishes them as PointCloud2 messages
for integration with Nav2 costmaps.

This node:
1. Subscribes to fisheye camera images
2. Detects objects using Gemini vision (or configurable backend)
3. Converts detected object positions to 3D points in robot frame
4. Publishes PointCloud2 for costmap integration

The PointCloud2 output can be added as an observation source in the
Nav2 costmap obstacle_layer configuration.

Usage:
    ros2 run maurice_nav vision_obstacle_detector --ros-args -p model:=gemini-2.0-flash
"""

import os
import sys
import math
import json
import struct
import threading
from typing import Optional, List, Tuple, Any
from dataclasses import dataclass

import numpy as np
import cv2

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import Image, PointCloud2, PointField
from std_msgs.msg import Header
from cv_bridge import CvBridge
from geometry_msgs.msg import TransformStamped
from tf2_ros import TransformBroadcaster

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Gemini client - we'll import lazily to allow running without it
GEMINI_AVAILABLE = False
try:
    from google import genai
    from google.genai import types
    GEMINI_AVAILABLE = True
except ImportError:
    pass


@dataclass
class DetectedObject:
    """Represents a detected object in the image."""
    class_name: str
    confidence: float
    bbox: Tuple[int, int, int, int]  # x1, y1, x2, y2 in pixels
    center_x: int  # pixel x
    center_y: int  # pixel y
    estimated_distance: Optional[float] = None  # meters, if available


@dataclass
class Object3D:
    """Object position in 3D robot frame."""
    class_name: str
    x: float  # forward (meters)
    y: float  # left (meters)
    z: float  # up (meters)
    radius: float  # estimated object radius (meters)


class VisionObstacleDetector(Node):
    """
    ROS2 node that detects obstacles from camera images and publishes PointCloud2.
    
    Subscribes to:
        - /mars/main_camera/image (or configured topic): Camera images
        
    Publishes:
        - /camera/obstacle_points: PointCloud2 of detected obstacles
        - /camera/detected_objects: Visualization markers (optional)
    """
    
    def __init__(self):
        super().__init__('vision_obstacle_detector')
        
        # Declare parameters
        self.declare_parameter('camera_topic', '/mars/main_camera/image')
        self.declare_parameter('output_topic', '/camera/obstacle_points')
        self.declare_parameter('model', 'gemini-2.0-flash')
        self.declare_parameter('detection_rate', 2.0)  # Hz
        self.declare_parameter('camera_height', 0.18)  # meters
        self.declare_parameter('camera_pitch', -15.0)  # degrees
        self.declare_parameter('camera_fov_h', 150.0)  # degrees (fisheye)
        self.declare_parameter('camera_fov_v', 120.0)  # degrees
        self.declare_parameter('max_detection_range', 3.0)  # meters
        self.declare_parameter('min_confidence', 0.5)
        self.declare_parameter('camera_frame', 'camera_link')
        self.declare_parameter('robot_frame', 'base_link')
        
        # Get parameters
        self.camera_topic = self.get_parameter('camera_topic').value
        self.output_topic = self.get_parameter('output_topic').value
        self.model_name = self.get_parameter('model').value
        self.detection_rate = self.get_parameter('detection_rate').value
        self.camera_height = self.get_parameter('camera_height').value
        self.camera_pitch = self.get_parameter('camera_pitch').value
        self.camera_fov_h = self.get_parameter('camera_fov_h').value
        self.camera_fov_v = self.get_parameter('camera_fov_v').value
        self.max_range = self.get_parameter('max_detection_range').value
        self.min_confidence = self.get_parameter('min_confidence').value
        self.camera_frame = self.get_parameter('camera_frame').value
        self.robot_frame = self.get_parameter('robot_frame').value
        
        # Image parameters (will be updated from first image)
        self.image_width = 640
        self.image_height = 480
        
        # Initialize CV bridge
        self.bridge = CvBridge()
        
        # Latest image storage
        self.latest_image: Optional[np.ndarray] = None
        self.image_lock = threading.Lock()
        self.image_stamp = None
        
        # Initialize Gemini client if available
        self.gemini_client = None
        if GEMINI_AVAILABLE:
            api_key = os.getenv("GEMINI_API_KEY")
            if api_key:
                try:
                    self.gemini_client = genai.Client(api_key=api_key)
                    self.get_logger().info(f"Gemini client initialized with model: {self.model_name}")
                except Exception as e:
                    self.get_logger().warn(f"Failed to initialize Gemini: {e}")
            else:
                self.get_logger().warn("GEMINI_API_KEY not set - object detection disabled")
        else:
            self.get_logger().warn("google-genai not installed - object detection disabled")
        
        # QoS for camera subscription (best effort for real-time)
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Subscribers
        self.image_sub = self.create_subscription(
            Image,
            self.camera_topic,
            self._image_callback,
            qos
        )
        
        # Publishers
        self.pointcloud_pub = self.create_publisher(
            PointCloud2,
            self.output_topic,
            10
        )
        
        # TF broadcaster for camera frame (if needed)
        self.tf_broadcaster = TransformBroadcaster(self)
        
        # Detection timer
        period = 1.0 / self.detection_rate
        self.detection_timer = self.create_timer(period, self._detection_callback)
        
        self.get_logger().info(
            f"Vision Obstacle Detector initialized:\n"
            f"  Camera topic: {self.camera_topic}\n"
            f"  Output topic: {self.output_topic}\n"
            f"  Detection rate: {self.detection_rate} Hz\n"
            f"  Camera height: {self.camera_height}m\n"
            f"  Max range: {self.max_range}m"
        )
    
    def _image_callback(self, msg: Image):
        """Store latest camera image."""
        with self.image_lock:
            try:
                self.latest_image = self.bridge.imgmsg_to_cv2(msg, 'bgr8')
                self.image_stamp = msg.header.stamp
                # Update image dimensions
                self.image_height, self.image_width = self.latest_image.shape[:2]
            except Exception as e:
                self.get_logger().error(f"Image conversion error: {e}")
    
    def _detection_callback(self):
        """Periodic detection callback."""
        # Get current image
        with self.image_lock:
            if self.latest_image is None:
                return
            image = self.latest_image.copy()
            stamp = self.image_stamp
        
        # Detect objects
        detected_objects = self._detect_objects(image)
        
        if not detected_objects:
            # Publish empty point cloud to clear old obstacles
            self._publish_empty_pointcloud(stamp)
            return
        
        # Convert to 3D points
        objects_3d = self._objects_to_3d(detected_objects)
        
        # Publish as PointCloud2
        self._publish_pointcloud(objects_3d, stamp)
        
        # Log detections
        if objects_3d:
            obj_strs = [f"{o.class_name}@{o.x:.1f}m" for o in objects_3d[:3]]
            self.get_logger().info(f"Detected: {', '.join(obj_strs)}")
    
    def _detect_objects(self, image: np.ndarray) -> List[DetectedObject]:
        """
        Detect objects in the image using Gemini vision.
        
        Override this method to use a different detection backend (YOLO, etc.)
        """
        if self.gemini_client is None:
            return []
        
        # Resize image for faster processing
        process_size = (320, 240)
        small_image = cv2.resize(image, process_size)
        
        # Convert to JPEG bytes
        _, jpeg_bytes = cv2.imencode('.jpg', small_image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        image_bytes = jpeg_bytes.tobytes()
        
        # Prompt for object detection
        prompt = """Detect obstacles and objects that a small ground robot should avoid.

Look for: furniture legs, chairs, walls, boxes, bags, shoes, toys, pets, cables, any objects on the floor.

Return JSON array of detected objects:
[{"class": "object_name", "confidence": 0.0-1.0, "bbox": [x1, y1, x2, y2]}]

Coordinates are normalized 0-1000 where [0,0] is top-left, [1000,1000] is bottom-right.
Only include objects with confidence > 0.5. If nothing detected, return empty array [].
Be conservative - better to detect false positives than miss obstacles."""

        try:
            response = self.gemini_client.models.generate_content(
                model=self.model_name,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt
                ],
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=512
                )
            )
            
            text = response.text.strip() if response.text else "[]"
            
            # Parse JSON
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
                if text.startswith("json"):
                    text = text[4:].strip()
            
            data = json.loads(text)
            
            # Convert to DetectedObject list
            objects = []
            scale_x = self.image_width / 1000.0
            scale_y = self.image_height / 1000.0
            
            for item in data:
                if item.get("confidence", 0) < self.min_confidence:
                    continue
                
                bbox = item.get("bbox", [0, 0, 0, 0])
                if len(bbox) != 4:
                    continue
                
                # Scale bbox to actual image size
                x1 = int(bbox[0] * scale_x)
                y1 = int(bbox[1] * scale_y)
                x2 = int(bbox[2] * scale_x)
                y2 = int(bbox[3] * scale_y)
                
                center_x = (x1 + x2) // 2
                center_y = (y1 + y2) // 2
                
                objects.append(DetectedObject(
                    class_name=str(item.get("class", "obstacle")),
                    confidence=float(item.get("confidence", 0.5)),
                    bbox=(x1, y1, x2, y2),
                    center_x=center_x,
                    center_y=center_y
                ))
            
            return objects
            
        except json.JSONDecodeError as e:
            self.get_logger().debug(f"JSON parse error: {e}")
        except Exception as e:
            self.get_logger().warn(f"Detection error: {e}")
        
        return []
    
    def _pixel_to_3d(self, px: int, py: int) -> Tuple[float, float, float]:
        """
        Convert pixel coordinates to 3D point in robot frame.
        
        Uses fisheye camera projection model to estimate ground intersection.
        
        Args:
            px: Pixel x coordinate
            py: Pixel y coordinate
            
        Returns:
            (x, y, z) in robot frame (forward, left, up) in meters
        """
        # Camera intrinsics for fisheye
        fx = (self.image_width / 2.0) / math.tan(math.radians(self.camera_fov_h / 2.0))
        fy = (self.image_height / 2.0) / math.tan(math.radians(self.camera_fov_v / 2.0))
        cx = self.image_width / 2.0
        cy = self.image_height / 2.0
        
        # Normalized image coordinates
        nx = (px - cx) / fx
        ny = (py - cy) / fy
        
        # Fisheye correction (approximate equidistant model)
        r = math.sqrt(nx * nx + ny * ny)
        if r > 0:
            fisheye_factor = 1.0 + 0.3 * r * r
            nx = nx / fisheye_factor
            ny = ny / fisheye_factor
        
        # Camera pitch adjustment
        pitch_rad = math.radians(self.camera_pitch)
        
        # Ray direction in camera frame
        ray_x = nx
        ray_y = ny
        ray_z = 1.0
        
        # Rotate by camera pitch (around X axis)
        cos_p = math.cos(pitch_rad)
        sin_p = math.sin(pitch_rad)
        
        ray_y_world = ray_y * cos_p - ray_z * sin_p
        ray_z_world = ray_y * sin_p + ray_z * cos_p
        
        # Intersect with ground plane (y_world corresponds to down in camera frame)
        if ray_y_world <= 0.001:
            # Ray not pointing down - assume fixed distance
            return (1.0, -nx * 0.5, 0.0)
        
        t = self.camera_height / ray_y_world
        
        # Ground intersection in robot frame
        # Forward = Z in camera, Right = X in camera
        forward = t * ray_z_world
        lateral = -t * ray_x  # Flip: positive = left in robot frame
        
        # Clamp to reasonable range
        forward = max(0.1, min(self.max_range, forward))
        lateral = max(-2.0, min(2.0, lateral))
        
        # Objects are on the ground (z=0) or slightly above
        return (forward, lateral, 0.05)  # 5cm above ground
    
    def _objects_to_3d(self, detected_objects: List[DetectedObject]) -> List[Object3D]:
        """Convert detected objects to 3D positions."""
        objects_3d = []
        
        for obj in detected_objects:
            # Get 3D position from object center
            x, y, z = self._pixel_to_3d(obj.center_x, obj.center_y)
            
            # Estimate object radius from bbox width
            bbox_width_pixels = obj.bbox[2] - obj.bbox[0]
            # Rough approximation: width in meters based on distance
            estimated_radius = max(0.05, (bbox_width_pixels / self.image_width) * x * 0.5)
            
            # Filter by range
            if x > self.max_range:
                continue
            
            objects_3d.append(Object3D(
                class_name=obj.class_name,
                x=x,
                y=y,
                z=z,
                radius=estimated_radius
            ))
        
        return objects_3d
    
    def _create_pointcloud2(
        self, 
        points: List[Tuple[float, float, float]], 
        stamp
    ) -> PointCloud2:
        """Create a PointCloud2 message from a list of 3D points."""
        msg = PointCloud2()
        msg.header = Header()
        msg.header.stamp = stamp if stamp else self.get_clock().now().to_msg()
        msg.header.frame_id = self.robot_frame
        
        # Define fields (x, y, z)
        msg.fields = [
            PointField(name='x', offset=0, datatype=PointField.FLOAT32, count=1),
            PointField(name='y', offset=4, datatype=PointField.FLOAT32, count=1),
            PointField(name='z', offset=8, datatype=PointField.FLOAT32, count=1),
        ]
        
        msg.is_bigendian = False
        msg.point_step = 12  # 3 floats * 4 bytes
        msg.height = 1
        msg.width = len(points)
        msg.row_step = msg.point_step * msg.width
        msg.is_dense = True
        
        # Pack points into bytes
        buffer = []
        for x, y, z in points:
            buffer.append(struct.pack('fff', x, y, z))
        
        msg.data = b''.join(buffer)
        
        return msg
    
    def _publish_pointcloud(self, objects_3d: List[Object3D], stamp):
        """Publish detected objects as PointCloud2."""
        # Generate multiple points per object to create "occupied" area
        points = []
        
        for obj in objects_3d:
            # Create a cluster of points around the object center
            # This helps the costmap see it as a real obstacle
            num_points = max(3, int(obj.radius * 20))  # More points for larger objects
            
            for i in range(num_points):
                angle = (2 * math.pi * i) / num_points
                # Points on the ground around the object
                px = obj.x + obj.radius * math.cos(angle) * 0.5
                py = obj.y + obj.radius * math.sin(angle) * 0.5
                pz = 0.0  # Ground level
                points.append((px, py, pz))
            
            # Also add center point at obstacle height
            points.append((obj.x, obj.y, 0.1))  # Slightly above ground
        
        if points:
            msg = self._create_pointcloud2(points, stamp)
            self.pointcloud_pub.publish(msg)
    
    def _publish_empty_pointcloud(self, stamp):
        """Publish empty point cloud (no obstacles detected)."""
        msg = self._create_pointcloud2([], stamp)
        self.pointcloud_pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = VisionObstacleDetector()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

