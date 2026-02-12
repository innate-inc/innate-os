#!/usr/bin/env python3
"""
Stereo Camera Calibration Node using ChArUco Board.

This launches an interactive calibration tool that:
1. Subscribes to the stereo camera topic
2. Allows user to capture images by pressing Enter
3. Detects ChArUco board corners in both cameras
4. Performs stereo calibration after collecting enough images
5. Optionally saves the calibration to replace the existing one


This node subscribes to a stereo image topic, allows the user to capture
calibration images interactively, and performs OpenCV stereo calibration
using the pinhole camera model.

Usage:
    ros2 run maurice_cam stereo_calibrator
    or
    ros2 run maurice_cam stereo_calibrator --ros-args -p squares_y:=8 -p squares_x:=11 -p square_size:=0.016 -p marker_size:=0.012

    Then press Enter to capture images. After 30 images, calibration is computed.
"""

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
import message_filters

from tf_transformations import euler_from_quaternion

from brain_client.manipulation_interface import ManipulationInterface
from maurice_cam.calibration_debug_vis import generate_debug_mosaic, generate_visualizations
from maurice_cam.calibration_utils import (
    setup_head_and_arm,
    save_calibration,
    prompt_save,
)


@dataclass
class DetectionResult:
    """Result of ChArUco detection on a stereo image pair."""

    success: bool = False
    num_common: int = 0

    # Input images (passed through for debug mosaic)
    left_img: Optional[np.ndarray] = None
    right_img: Optional[np.ndarray] = None

    # ArUco marker detections
    marker_corners_left: Optional[tuple] = None
    marker_ids_left: Optional[np.ndarray] = None
    marker_corners_right: Optional[tuple] = None
    marker_ids_right: Optional[np.ndarray] = None

    # ChArUco corner detections
    charuco_corners_left: Optional[np.ndarray] = None
    charuco_ids_left: Optional[np.ndarray] = None
    charuco_corners_right: Optional[np.ndarray] = None
    charuco_ids_right: Optional[np.ndarray] = None

    # Filtered common corners & 3D object points (for stereoCalibrate)
    common_ids: Optional[set] = None
    corners_left_filtered: Optional[np.ndarray] = None
    corners_right_filtered: Optional[np.ndarray] = None
    obj_pts_common: Optional[np.ndarray] = None


class StereoCalibrator(Node):
    """ROS2 node for interactive stereo camera calibration using ChArUco boards."""

    def __init__(self):
        super().__init__('stereo_calibrator')

        # Declare parameters
        self.declare_parameter('left_topic', '/mars/main_camera/left/image_raw')
        self.declare_parameter('right_topic', '/mars/main_camera/right/image_raw')
        self.declare_parameter('image_width', 640)
        self.declare_parameter('image_height', 480)
        self.declare_parameter('data_directory', '/home/jetson1/innate-os/data')
        
        # ChArUco board parameters
        self.declare_parameter('squares_x', 17)   # 8 squares wide
        self.declare_parameter('squares_y', 9)   # 11 squares tall
        self.declare_parameter('square_size', 0.016)  # 16mm in meters
        self.declare_parameter('marker_size', 0.012)  # 12mm in meters
        self.declare_parameter('dictionary_id', cv2.aruco.DICT_4X4_250)
        
        # Calibration parameters
        self.declare_parameter('num_images', 30)
        self.declare_parameter('min_corners', 10)  # Minimum corners to accept an image (recommend 10+ for calibrateCamera)
        self.declare_parameter('use_legacy_pattern', True)  # Enable for calib.io boards (OpenCV 4.6.0+)
        self.declare_parameter('debug', False)  # Enable debug mosaic after each capture
        self.declare_parameter('save_fk_pose', False)  # Save FK pose alongside captured images

        # Auto-move parameters
        self.declare_parameter('auto_move', False)  # Enable automatic arm movement through pre-recorded poses
        self.declare_parameter('auto_move_interval', 5)  # Seconds between moves
        self.declare_parameter('auto_capture_delay', 2.0)  # Seconds to wait after move before capture

        # Get parameters
        self.left_topic = self.get_parameter('left_topic').value
        self.right_topic = self.get_parameter('right_topic').value
        self.image_width = self.get_parameter('image_width').value
        self.image_height = self.get_parameter('image_height').value
        self.data_directory = Path(self.get_parameter('data_directory').value)
        
        self.squares_x = self.get_parameter('squares_x').value
        self.squares_y = self.get_parameter('squares_y').value
        self.square_size = self.get_parameter('square_size').value
        self.marker_size = self.get_parameter('marker_size').value
        self.dictionary_id = self.get_parameter('dictionary_id').value
        
        self.num_images_required = self.get_parameter('num_images').value
        self.min_corners = self.get_parameter('min_corners').value
        self.use_legacy_pattern = self.get_parameter('use_legacy_pattern').value
        self.debug = self.get_parameter('debug').value
        self.save_fk_pose = self.get_parameter('save_fk_pose').value

        # Auto-move params
        self.auto_move = self.get_parameter('auto_move').value
        self.auto_move_interval = self.get_parameter('auto_move_interval').value
        self.auto_capture_delay = self.get_parameter('auto_capture_delay').value

        # Create ChArUco board
        self.aruco_dict = cv2.aruco.getPredefinedDictionary(self.dictionary_id)
        self.charuco_board = cv2.aruco.CharucoBoard(
            (self.squares_x, self.squares_y),
            self.square_size,
            self.marker_size,
            self.aruco_dict
        )
        # Enable legacy pattern for calib.io boards (OpenCV 4.6.0+ changed pattern format)
        if self.use_legacy_pattern:
            self.charuco_board.setLegacyPattern(True)
        self.charuco_detector = cv2.aruco.CharucoDetector(self.charuco_board)

        # Storage for calibration data
        # Per-camera: ALL detected corners (for individual calibrateCamera)
        self.indiv_corners_left = []
        self.indiv_corners_right = []
        self.indiv_obj_points_left = []
        self.indiv_obj_points_right = []
        # Common: only corners seen in BOTH cameras (for stereoCalibrate)
        self.common_corners_left = []
        self.common_corners_right = []
        self.common_obj_points = []

        # State
        self.bridge = CvBridge()
        self.latest_left_frame = None
        self.latest_right_frame = None
        self.frame_lock = threading.Lock()
        self.capture_requested = False
        self.images_captured = 0
        self.capture_attempts = 0
        self.calibration_done = False

        # Image storage directory - inside data directory so images persist
        self.tmp_image_dir = self.data_directory / 'stereo_calibration_images'
        self.tmp_image_dir.mkdir(parents=True, exist_ok=True)

        # Set up head and arm for calibration
        setup_head_and_arm(self)

        # Initialize ManipulationInterface for arm control
        self.manipulation = ManipulationInterface(self, self.get_logger())

        # Auto-move state
        self.auto_move_poses = []  # List of (x, y, z, roll, pitch, yaw) tuples
        self.auto_move_index = 0
        self.auto_move_active = False  # True while an auto-move sequence is running
        if self.auto_move:
            self.auto_move_poses = self._load_fk_poses()

        # Check for existing images and ask user
        self.check_existing_images()

        # Print info
        self.get_logger().info('=' * 60)
        self.get_logger().info('Stereo Camera Calibrator')
        self.get_logger().info('=' * 60)
        self.get_logger().info(f'Left topic: {self.left_topic}')
        self.get_logger().info(f'Right topic: {self.right_topic}')
        self.get_logger().info(f'Using ManipulationInterface for arm control')
        if self.auto_move and self.auto_move_poses:
            self.get_logger().info(f'Auto-move ENABLED: {len(self.auto_move_poses)} poses, '
                                   f'interval={self.auto_move_interval}s, capture_delay={self.auto_capture_delay}s')
        self.get_logger().info(f'Image size: {self.image_width}x{self.image_height} per camera')
        self.get_logger().info(f'ChArUco board: {self.squares_x}x{self.squares_y}')
        self.get_logger().info(f'Square size: {self.square_size*1000:.1f}mm, Marker size: {self.marker_size*1000:.1f}mm')
        self.get_logger().info(f'Images required: {self.num_images_required}')
        if self.use_legacy_pattern:
            self.get_logger().info('Legacy pattern enabled (for calib.io boards)')

        # Create synchronized subscriptions for left and right images
        self.left_sub = message_filters.Subscriber(self, Image, self.left_topic)
        self.right_sub = message_filters.Subscriber(self, Image, self.right_topic)
        
        # Use ApproximateTimeSynchronizer since timestamps may differ slightly
        self.sync = message_filters.ApproximateTimeSynchronizer(
            [self.left_sub, self.right_sub],
            queue_size=10,
            slop=0.1  # 100ms tolerance
        )
        self.sync.registerCallback(self.image_callback)

        # Start keyboard input thread (only when auto-move is disabled)
        if not self.auto_move or not self.auto_move_poses:
            self.input_thread = threading.Thread(target=self.keyboard_input_loop, daemon=True)
            self.input_thread.start()

        # Create timer to process captures
        self.timer = self.create_timer(0.1, self.process_capture)

        # Start auto-move sequence if enabled
        if self.auto_move and self.auto_move_poses:
            self.get_logger().info('Starting auto-move sequence in background thread...')
            self.auto_move_thread = threading.Thread(target=self._auto_move_loop, daemon=True)
            self.auto_move_thread.start()
        else:
            self.get_logger().info('=' * 60)
            self.get_logger().warn('>>> Move the arm out of the way then Press ENTER to capture an image <<<')

    def image_callback(self, left_msg, right_msg):
        """Store latest left and right frames."""
        try:
            # Convert left image
            if left_msg.encoding == 'bgr8':
                left_frame = self.bridge.imgmsg_to_cv2(left_msg, 'bgr8')
            elif left_msg.encoding == 'rgb8':
                left_frame = self.bridge.imgmsg_to_cv2(left_msg, 'rgb8')
                left_frame = cv2.cvtColor(left_frame, cv2.COLOR_RGB2BGR)
            else:
                left_frame = self.bridge.imgmsg_to_cv2(left_msg, 'mono8')
                left_frame = cv2.cvtColor(left_frame, cv2.COLOR_GRAY2BGR)
            
            # Convert right image
            if right_msg.encoding == 'bgr8':
                right_frame = self.bridge.imgmsg_to_cv2(right_msg, 'bgr8')
            elif right_msg.encoding == 'rgb8':
                right_frame = self.bridge.imgmsg_to_cv2(right_msg, 'rgb8')
                right_frame = cv2.cvtColor(right_frame, cv2.COLOR_RGB2BGR)
            else:
                right_frame = self.bridge.imgmsg_to_cv2(right_msg, 'mono8')
                right_frame = cv2.cvtColor(right_frame, cv2.COLOR_GRAY2BGR)
            
            # Debug: log frame info on first frame
            if self.latest_left_frame is None:
                self.get_logger().info(
                    f'Received first frames: left={left_frame.shape[1]}x{left_frame.shape[0]}, '
                    f'right={right_frame.shape[1]}x{right_frame.shape[0]}, '
                    f'expected={self.image_width}x{self.image_height}'
                )
            
            # Store frames
            with self.frame_lock:
                self.latest_left_frame = left_frame
                self.latest_right_frame = right_frame
                
        except Exception as e:
            self.get_logger().error(f'Failed to convert image: {e}')

    def _save_fk_pose(self) -> None:
        """Append the current FK pose to the fk_poses.json array file."""
        ee_pose = self.manipulation.get_current_end_effector_pose()
        if ee_pose is None:
            self.get_logger().warn('FK pose not available; skipping FK pose save for this capture.')
            return

        fk_path = self.tmp_image_dir / 'fk_poses.json'
        try:
            # Load existing array or start fresh
            if fk_path.exists():
                with fk_path.open('r', encoding='utf-8') as f:
                    poses_list = json.load(f)
            else:
                poses_list = []

            poses_list.append(ee_pose)

            with fk_path.open('w', encoding='utf-8') as f:
                json.dump(poses_list, f, indent=2)
        except Exception as e:
            self.get_logger().warn(f'Failed to save FK pose: {e}')

    def _load_fk_poses(self) -> list:
        """Load pre-recorded FK poses from fk_poses.json and convert to
        (x, y, z, roll, pitch, yaw) tuples.

        Each entry in the JSON array should have:
          {"position": {"x", "y", "z"}, "orientation": {"x", "y", "z", "w"}, ...}

        Returns:
            List of (x, y, z, roll, pitch, yaw) tuples.
        """
        fk_path = self.tmp_image_dir / 'fk_poses.json'
        if not fk_path.exists():
            self.get_logger().warn(f'FK poses file not found: {fk_path}')
            return []

        try:
            with fk_path.open('r', encoding='utf-8') as f:
                poses_list = json.load(f)
        except Exception as e:
            self.get_logger().warn(f'Failed to read FK poses file: {e}')
            return []

        poses = []
        for i, entry in enumerate(poses_list):
            try:
                # Support both direct {position, orientation} and wrapped {pose: {position, orientation}}
                pose = entry.get('pose', entry) if isinstance(entry, dict) else entry
                pos = pose['position']
                ori = pose['orientation']

                x, y, z = pos['x'], pos['y'], pos['z']
                qx, qy, qz, qw = ori['x'], ori['y'], ori['z'], ori['w']
                roll, pitch, yaw = euler_from_quaternion([qx, qy, qz, qw])

                poses.append((x, y, z, roll, pitch, yaw))
            except Exception as e:
                self.get_logger().warn(f'Failed to parse FK pose entry {i}: {e}')

        self.get_logger().info(f'Loaded {len(poses)} FK poses from {fk_path}')
        return poses

    def _auto_move_loop(self):
        """Background thread: iterate through pre-recorded poses, move arm, and auto-capture."""
        self.get_logger().info(f'Auto-move: starting with {len(self.auto_move_poses)} poses')

        # Ensure gripper is closed before moving
        self.get_logger().info('Auto-move: closing gripper...')
        self.manipulation.close_gripper(duration=1.0)
        time.sleep(1.5)

        while rclpy.ok() and not self.calibration_done and self.auto_move_index < len(self.auto_move_poses):
            pose = self.auto_move_poses[self.auto_move_index]
            x, y, z, roll, pitch, yaw = pose
            idx = self.auto_move_index + 1
            total = len(self.auto_move_poses)

            self.get_logger().info(
                f'Auto-move [{idx}/{total}]: Moving to pose '
                f'(x={x:.3f}, y={y:.3f}, z={z:.3f}, '
                f'r={np.degrees(roll):.1f}°, p={np.degrees(pitch):.1f}°, y={np.degrees(yaw):.1f}°)'
            )

            success = self.manipulation.move_to_cartesian_pose(
                x, y, z, roll, pitch, yaw, duration=3, ik_timeout=3.0
            )

            if not success:
                self.get_logger().warn(f'Auto-move [{idx}/{total}]: Move failed, skipping pose')
                self.auto_move_index += 1
                continue

            # Wait for the arm to finish the trajectory + settle time
            self.get_logger().info(
                f'Auto-move [{idx}/{total}]: Move issued, waiting {self.auto_capture_delay}s before capture...'
            )
            time.sleep(self.auto_capture_delay)

            # Request capture (processed by the main timer callback)
            self.capture_requested = True
            self.get_logger().info(f'Auto-move [{idx}/{total}]: Capture requested')

            # Wait for the capture to be processed before moving on
            while self.capture_requested and rclpy.ok() and not self.calibration_done:
                time.sleep(0.1)

            self.auto_move_index += 1

            # Wait the interval before the next move (unless calibration is done)
            if not self.calibration_done and self.auto_move_index < len(self.auto_move_poses):
                self.get_logger().info(
                    f'Auto-move: waiting {self.auto_move_interval}s before next pose...'
                )
                time.sleep(self.auto_move_interval)

        if self.calibration_done:
            self.get_logger().info('Auto-move: stopping — calibration complete')
        elif self.auto_move_index >= len(self.auto_move_poses):
            self.get_logger().info('Auto-move: all poses exhausted')
            # Wait for the last capture to be processed
            while self.capture_requested and rclpy.ok():
                time.sleep(0.1)
            if not self.calibration_done and self.images_captured > 0:
                self.get_logger().info(
                    f'Auto-move: running calibration with {self.images_captured} images '
                    f'(requested {self.num_images_required})'
                )
                self.run_calibration()

    def check_existing_images(self):
        """Check for existing calibration images in /tmp and ask user if they want to use them."""
        # Find all left images
        left_images = sorted(self.tmp_image_dir.glob('left_*.png'))
        right_images = sorted(self.tmp_image_dir.glob('right_*.png'))
        
        if len(left_images) >= self.num_images_required and len(right_images) >= self.num_images_required:
            self.get_logger().info('')
            self.get_logger().info(f'Found {len(left_images)} existing calibration images in {self.tmp_image_dir}')
            print('')
            print(f'Found {len(left_images)} existing calibration images in {self.tmp_image_dir}')
            print('Do you want to use these images for calibration?')
            print('Type "y" to use existing images, "n" to capture new ones: ', end='', flush=True)
            
            try:
                response = input().strip().lower()
                if response == 'y':
                    self.get_logger().info('Loading existing images...')
                    self.load_existing_images(left_images[:self.num_images_required], 
                                            right_images[:self.num_images_required])
                    return
                else:
                    # User chose to override - delete old images
                    self.get_logger().info('Deleting old calibration images...')
                    for img in left_images:
                        img.unlink()
                    for img in right_images:
                        img.unlink()
                    self.get_logger().info(f'Deleted {len(left_images) + len(right_images)} old images.')
            except EOFError:
                self.get_logger().info('No input, proceeding with new capture.')
            except Exception as e:
                self.get_logger().warn(f'Error reading input: {e}, proceeding with new capture.')
        
        # If we get here, proceed with normal capture
        self.get_logger().info('')
        self.get_logger().warn('>>> Press ENTER to capture an image <<<')
        self.get_logger().info('')

    def load_existing_images(self, left_image_paths, right_image_paths):
        """Load and process existing calibration images."""
        self.get_logger().info(f'Processing {len(left_image_paths)} existing images...')
        
        for idx, (left_path, right_path) in enumerate(zip(left_image_paths, right_image_paths), 1):
            try:
                left_img = cv2.imread(str(left_path))
                right_img = cv2.imread(str(right_path))
                
                if left_img is None or right_img is None:
                    self.get_logger().warn(f'Failed to load images: {left_path}, {right_path}')
                    continue
                
                result = self._process_image_pair(left_img, right_img, label=f'image {idx}')

                if result.success:
                    self.get_logger().info(
                        f'[{self.images_captured}/{self.num_images_required}] '
                        f'Loaded image {idx}: {result.num_common} common corners.'
                    )
                    if self.images_captured >= self.num_images_required:
                        self.get_logger().info('')
                        self.get_logger().info('All images loaded! Computing calibration...')
                        self.calibration_done = True
                        self.run_calibration()
                        return
                        
            except Exception as e:
                self.get_logger().error(f'Error processing image {idx}: {e}')
                continue
        
        # If we get here, we didn't get enough valid images
        if self.images_captured < self.num_images_required:
            self.get_logger().warn(
                f'Only loaded {self.images_captured} valid images out of {self.num_images_required} required.'
            )
            self.get_logger().warn('Please capture new images.')
            self.images_captured = 0
            self.indiv_corners_left = []
            self.indiv_corners_right = []
            self.indiv_obj_points_left = []
            self.indiv_obj_points_right = []
            self.common_corners_left = []
            self.common_corners_right = []
            self.common_obj_points = []
            self.calibration_done = False
            self.get_logger().info('')
            self.get_logger().info('>>> Press ENTER to capture an image <<<')
            self.get_logger().info('')

    def keyboard_input_loop(self):
        """Background thread to handle keyboard input."""
        while rclpy.ok() and not self.calibration_done:
            try:
                # Wait for Enter key
                input('Slightly move the camera/robot and press Enter to capture the next image')
                if not self.calibration_done:
                    self.capture_requested = True
            except EOFError:
                break

    def _process_image_pair(self, left_img, right_img, label='capture', save_images=False) -> DetectionResult:
        """Detect ChArUco corners in a stereo image pair and store if valid.

        This is the shared detection/filtering/storage pipeline used by both
        live capture (``process_capture``) and offline reload
        (``load_existing_images``).

        Args:
            left_img: BGR left image.
            right_img: BGR right image.
            label: Human-readable label for log messages (e.g. 'capture', 'image 5').
            save_images: If True, save accepted images to ``tmp_image_dir``.

        Returns:
            DetectionResult with all detection data and success status.
        """
        # Convert to grayscale for detection
        left_gray = cv2.cvtColor(left_img, cv2.COLOR_BGR2GRAY)
        right_gray = cv2.cvtColor(right_img, cv2.COLOR_BGR2GRAY)

        left_mean = np.mean(left_gray)
        right_mean = np.mean(right_gray)
        self.get_logger().debug(
            f'Image brightness - Left mean: {left_mean:.1f}, Right mean: {right_mean:.1f}'
        )

        # Detect ChArUco corners
        # detectBoard returns: (charuco_corners, charuco_ids, marker_corners, marker_ids)
        # - marker_corners/marker_ids: ArUco markers detected (the black squares with patterns)
        # - charuco_corners/charuco_ids: Chessboard corners interpolated from markers (the intersections)
        # 
        # How it works:
        # 1. First, ArUco markers are detected (this is easier - they're distinct patterns)
        # 2. Then, chessboard corners are interpolated from the detected markers
        # 3. If markers are detected but corners aren't, it means the corner interpolation failed
        #    This happens when markers aren't in a complete grid or board parameters don't match
        
        # Use detectBoard which handles both marker detection and corner interpolation
        charuco_corners_left, charuco_ids_left, marker_corners_left, marker_ids_left = \
            self.charuco_detector.detectBoard(left_gray)
        charuco_corners_right, charuco_ids_right, marker_corners_right, marker_ids_right = \
            self.charuco_detector.detectBoard(right_gray)

        left_markers = len(marker_ids_left) if marker_ids_left is not None else 0
        right_markers = len(marker_ids_right) if marker_ids_right is not None else 0
        left_corners = len(charuco_ids_left) if charuco_ids_left is not None else 0
        right_corners = len(charuco_ids_right) if charuco_ids_right is not None else 0

        self.get_logger().info(
            f'Detection results - Left: {left_markers} markers, {left_corners} corners | '
            f'Right: {right_markers} markers, {right_corners} corners'
        )

        # Explain why corners might not be interpolated
        if left_markers > 0 and left_corners == 0:
            self.get_logger().warn(
                f'Left: {left_markers} markers detected but 0 corners interpolated. '
                f'Possible causes: markers not in complete grid, board partially visible, '
                f'or board parameters (squares_x={self.squares_x}, squares_y={self.squares_y}) don\'t match.'
            )
        if right_markers > 0 and right_corners == 0:
            self.get_logger().warn(
                f'Right: {right_markers} markers detected but 0 corners interpolated. '
                f'Possible causes: markers not in complete grid, board partially visible, '
                f'or board parameters (squares_x={self.squares_x}, squares_y={self.squares_y}) don\'t match.'
            )

        # Build result (populated with failure defaults)
        result = DetectionResult(
            left_img=left_img,
            right_img=right_img,
            marker_corners_left=marker_corners_left,
            marker_ids_left=marker_ids_left,
            marker_corners_right=marker_corners_right,
            marker_ids_right=marker_ids_right,
            charuco_corners_left=charuco_corners_left,
            charuco_ids_left=charuco_ids_left,
            charuco_corners_right=charuco_corners_right,
            charuco_ids_right=charuco_ids_right,
        )

        # Check minimum corner count
        if left_corners < self.min_corners or right_corners < self.min_corners:
            self.get_logger().warn(
                f'{label}: Not enough corners detected! Left: {left_corners}, Right: {right_corners} '
                f'(need {self.min_corners}+).'
            )
            self.get_logger().warn(
                f'  Image brightness - Left: {left_mean:.1f}, Right: {right_mean:.1f} '
                f'(typical range: 50-200)'
            )
            self.get_logger().warn(
                f'  Markers detected - Left: {left_markers}, Right: {right_markers}'
            )
            self.get_logger().warn(
                f'  Make sure ChArUco board ({self.squares_x}x{self.squares_y}) is fully visible '
                f'in BOTH camera views with good lighting.'
            )
            # Save diagnostic images
            try:
                debug_dir = self.data_directory / 'calibration_debug'
                debug_dir.mkdir(parents=True, exist_ok=True)
                import time
                timestamp = int(time.time())
                cv2.imwrite(str(debug_dir / f'left_{timestamp}.png'), left_img)
                cv2.imwrite(str(debug_dir / f'right_{timestamp}.png'), right_img)
                self.get_logger().info(f'  Saved diagnostic images to: {debug_dir}')
            except Exception as e:
                self.get_logger().debug(f'Could not save diagnostic images: {e}')
            return result

        # Find common corner IDs between left and right
        if charuco_ids_left is None or charuco_ids_right is None:
            self.get_logger().warn(f'{label}: ChArUco board not detected in one or both images.')
            return result

        left_ids_set = set(charuco_ids_left.flatten())
        right_ids_set = set(charuco_ids_right.flatten())
        common_ids = left_ids_set & right_ids_set

        if len(common_ids) < self.min_corners:
            self.get_logger().warn(
                f'{label}: Not enough common corners! Common: {len(common_ids)} '
                f'(need {self.min_corners}+).'
            )
            return result

        # Filter to keep only common corners, sorted by ID
        left_mask = [i for i, id_val in enumerate(charuco_ids_left.flatten())
                     if id_val in common_ids]
        right_mask = [i for i, id_val in enumerate(charuco_ids_right.flatten())
                      if id_val in common_ids]

        left_order = np.argsort(charuco_ids_left.flatten()[left_mask])
        right_order = np.argsort(charuco_ids_right.flatten()[right_mask])

        corners_left_filtered = charuco_corners_left[left_mask][left_order]
        corners_right_filtered = charuco_corners_right[right_mask][right_order]
        ids_filtered = charuco_ids_left[left_mask][left_order]

        # Get 3D object points
        all_board_corners = self.charuco_board.getChessboardCorners()
        obj_pts_common = all_board_corners[ids_filtered.flatten()]
        obj_pts_left = all_board_corners[charuco_ids_left.flatten()]
        obj_pts_right = all_board_corners[charuco_ids_right.flatten()]

        # Store per-camera individual points (for calibrateCamera)
        self.indiv_corners_left.append(charuco_corners_left)
        self.indiv_corners_right.append(charuco_corners_right)
        self.indiv_obj_points_left.append(obj_pts_left.reshape(-1, 1, 3))
        self.indiv_obj_points_right.append(obj_pts_right.reshape(-1, 1, 3))

        # Store common points (for stereoCalibrate)
        self.common_corners_left.append(corners_left_filtered)
        self.common_corners_right.append(corners_right_filtered)
        self.common_obj_points.append(obj_pts_common.reshape(-1, 1, 3))

        self.images_captured += 1

        # Optionally save images to disk
        if save_images:
            try:
                cv2.imwrite(str(self.tmp_image_dir / f'left_{self.images_captured:03d}.png'), left_img)
                cv2.imwrite(str(self.tmp_image_dir / f'right_{self.images_captured:03d}.png'), right_img)
                if self.save_fk_pose:
                    self._save_fk_pose()
            except Exception as e:
                self.get_logger().warn(f'Failed to save images: {e}')

        result.success = True
        result.num_common = len(common_ids)
        result.common_ids = common_ids
        result.corners_left_filtered = corners_left_filtered
        result.corners_right_filtered = corners_right_filtered
        result.obj_pts_common = obj_pts_common
        return result

    def process_capture(self):
        """Process capture request in main thread."""
        if self.calibration_done:
            return

        if not self.capture_requested:
            return
        
        self.capture_requested = False

        with self.frame_lock:
            if self.latest_left_frame is None or self.latest_right_frame is None:
                self.get_logger().warn('No frames available yet. Make sure the camera is running.')
                return
            left_img = self.latest_left_frame.copy()
            right_img = self.latest_right_frame.copy()

        self.capture_attempts += 1
        result = self._process_image_pair(left_img, right_img, label='Capture', save_images=True)

        if result.success:
            self.get_logger().info(
                f'[{self.images_captured}/{self.num_images_required}] '
                f'Captured! Detected {result.num_common} common corners.'
            )

        # Generate debug mosaic after every capture attempt
        if self.debug:
            generate_debug_mosaic(self, result)

        # Check if we have enough images
        if result.success and self.images_captured >= self.num_images_required:
            self.get_logger().info('')
            self.get_logger().info('All images captured! Computing calibration...')
            self.run_calibration()

    def run_calibration(self):
        """Run stereo calibration using collected data."""
        self.calibration_done = True
        
        image_size = (self.image_width, self.image_height)
        
        self.get_logger().info('Running individual camera calibrations...')
        self.get_logger().info(
            f'  Individual points: {len(self.indiv_obj_points_left)} images, '
            f'Common points: {len(self.common_obj_points)} images'
        )
        
        # # Debug: check shapes
        # for i, (obj_l, corners_l) in enumerate(zip(self.indiv_obj_points_left, self.indiv_corners_left)):
        #     self.get_logger().debug(f'  Left  img {i+1}: obj={obj_l.shape}, corners={corners_l.shape}')
        # for i, (obj_r, corners_r) in enumerate(zip(self.indiv_obj_points_right, self.indiv_corners_right)):
        #     self.get_logger().debug(f'  Right img {i+1}: obj={obj_r.shape}, corners={corners_r.shape}')
        
        # Calibrate left camera using ALL left-camera corners (not just common)
        ret_left, K1, D1, rvecs_left, tvecs_left = cv2.calibrateCamera(
            self.indiv_obj_points_left,
            self.indiv_corners_left,
            image_size,
            None, None,
            flags=0
        )
        self.get_logger().info(f'Left camera RMS error: {ret_left:.4f}')

        # Calibrate right camera using ALL right-camera corners (not just common)
        ret_right, K2, D2, rvecs_right, tvecs_right = cv2.calibrateCamera(
            self.indiv_obj_points_right,
            self.indiv_corners_right,
            image_size,
            None, None,
            flags=0
        )
        self.get_logger().info(f'Right camera RMS error: {ret_right:.4f}')

        self.get_logger().info('Running stereo calibration...')
        
        # Stereo calibration uses COMMON corners only (matched across both cameras)
        flags = cv2.CALIB_FIX_INTRINSIC
        
        ret_stereo, K1, D1, K2, D2, R, T, E, F = cv2.stereoCalibrate(
            self.common_obj_points,
            self.common_corners_left,
            self.common_corners_right,
            K1, D1,
            K2, D2,
            image_size,
            flags=flags,
            criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6)
        )
        
        # Ensure T[0] is positive (left camera physically left of right camera)
        # If negative, cameras are physically swapped - negate to fix depth sign
        if T[0, 0] < 0:
            self.get_logger().warn(f'T[0] = {T[0,0]:.4f}m is negative - cameras may be physically swapped')
            self.get_logger().warn('Negating T to ensure positive depth output')
            T = -T
        
        self.get_logger().info(f'Stereo RMS error: {ret_stereo:.4f}')

        # Compute rectification transforms
        R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(
            K1, D1, K2, D2, image_size, R, T,
            alpha=0,  # 0 = crop to valid pixels only
            flags=cv2.CALIB_ZERO_DISPARITY
        )

        # Calculate baseline
        baseline = np.linalg.norm(T)
        focal_length = Q[2, 3]

        # Print results
        self.get_logger().info('')
        self.get_logger().info('=' * 60)
        self.get_logger().info('CALIBRATION RESULTS')
        self.get_logger().info('=' * 60)
        self.get_logger().info(f'Left camera RMS:   {ret_left:.4f} pixels')
        self.get_logger().info(f'Right camera RMS:  {ret_right:.4f} pixels')
        self.get_logger().info(f'Stereo RMS:        {ret_stereo:.4f} pixels')
        self.get_logger().info(f'Baseline:          {baseline*1000:.2f} mm')
        self.get_logger().info(f'Focal length:      {focal_length:.2f} pixels')
        self.get_logger().info('=' * 60)
        self.get_logger().info('')

        # Quality assessment
        if ret_stereo < 0.5:
            quality = "EXCELLENT"
        elif ret_stereo < 1.0:
            quality = "GOOD"
        elif ret_stereo < 2.0:
            quality = "ACCEPTABLE"
        else:
            quality = "POOR - consider recalibrating"
        
        self.get_logger().info(f'Calibration quality: {quality}')
        self.get_logger().info('')

        # Store calibration data for saving (must be before generate_visualizations)
        self.calibration_data = {
            'K1': K1,
            'D1': D1,
            'K2': K2,
            'D2': D2,
            'R': R,
            'T': T,
            'R1': R1,
            'R2': R2,
            'P1': P1,
            'P2': P2,
            'Q': Q,
            'image_width': self.image_width,
            'image_height': self.image_height,
            'rms_error': ret_stereo,
        }

        # Generate visualization images
        self.get_logger().info('Generating visualization images...')
        generate_visualizations(self)

        # Ask user if they want to save
        prompt_save(self)



def main(args=None):
    rclpy.init(args=args)
    
    node = StereoCalibrator()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # "context is not valid" is expected on SIGTERM / normal shutdown — ignore it
        if 'context is not valid' not in str(e):
            print(f'[stereo_calibrator] Error in main: {e}', file=sys.stderr)

    try:
        node.destroy_node()
    except Exception:
        pass
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == '__main__':
    main()

