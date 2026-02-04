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
    ros2 run maurice_cam stereo_calibrator --ros-args -p squares_y:=11 -p squares_x:=8 -p square_size:=0.016 -p marker_size:=0.012

    Then press Enter to capture images. After 30 images, calibration is computed.
"""

import sys
import select
import termios
import tty
import threading
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
import message_filters


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
        self.declare_parameter('squares_x', 8)   # 8 squares wide
        self.declare_parameter('squares_y', 11)   # 11 squares tall
        self.declare_parameter('square_size', 0.016)  # 16mm in meters
        self.declare_parameter('marker_size', 0.012)  # 12mm in meters
        self.declare_parameter('dictionary_id', cv2.aruco.DICT_4X4_250)
        
        # Calibration parameters
        self.declare_parameter('num_images', 30)
        self.declare_parameter('min_corners', 20)  # Minimum corners to accept an image
        self.declare_parameter('use_legacy_pattern', True)  # Enable for calib.io boards (OpenCV 4.6.0+)

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
        self.all_corners_left = []
        self.all_corners_right = []
        self.all_ids_left = []
        self.all_ids_right = []
        self.obj_points = []  # 3D object points for stereo calibration

        # State
        self.bridge = CvBridge()
        self.latest_left_frame = None
        self.latest_right_frame = None
        self.frame_lock = threading.Lock()
        self.capture_requested = False
        self.images_captured = 0
        self.calibration_done = False

        # Image storage directory
        self.tmp_image_dir = Path('/tmp/stereo_calibration_images')
        self.tmp_image_dir.mkdir(parents=True, exist_ok=True)

        # Check for existing images and ask user
        self.check_existing_images()

        # Print info
        self.get_logger().info('=' * 60)
        self.get_logger().info('Stereo Camera Calibrator')
        self.get_logger().info('=' * 60)
        self.get_logger().info(f'Left topic: {self.left_topic}')
        self.get_logger().info(f'Right topic: {self.right_topic}')
        self.get_logger().info(f'Image size: {self.image_width}x{self.image_height} per camera')
        self.get_logger().info(f'ChArUco board: {self.squares_x}x{self.squares_y}')
        self.get_logger().info(f'Square size: {self.square_size*1000:.1f}mm, Marker size: {self.marker_size*1000:.1f}mm')
        self.get_logger().info(f'Images required: {self.num_images_required}')
        if self.use_legacy_pattern:
            self.get_logger().info('Legacy pattern enabled (for calib.io boards)')

        self.get_logger().info('=' * 60)
        self.get_logger().warn('>>> Press ENTER to capture an image <<<')

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

        # Start keyboard input thread
        self.input_thread = threading.Thread(target=self.keyboard_input_loop, daemon=True)
        self.input_thread.start()

        # Create timer to process captures
        self.timer = self.create_timer(0.1, self.process_capture)

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
                # Load images
                left_img = cv2.imread(str(left_path))
                right_img = cv2.imread(str(right_path))
                
                if left_img is None or right_img is None:
                    self.get_logger().warn(f'Failed to load images: {left_path}, {right_path}')
                    continue
                
                # Convert to grayscale
                left_gray = cv2.cvtColor(left_img, cv2.COLOR_BGR2GRAY)
                right_gray = cv2.cvtColor(right_img, cv2.COLOR_BGR2GRAY)
                
                # Detect ChArUco corners
                charuco_corners_left, charuco_ids_left, marker_corners_left, marker_ids_left = \
                    self.charuco_detector.detectBoard(left_gray)
                charuco_corners_right, charuco_ids_right, marker_corners_right, marker_ids_right = \
                    self.charuco_detector.detectBoard(right_gray)
                
                # Check if enough corners detected
                left_count = len(charuco_ids_left) if charuco_ids_left is not None else 0
                right_count = len(charuco_ids_right) if charuco_ids_right is not None else 0
                
                if left_count < self.min_corners or right_count < self.min_corners:
                    self.get_logger().warn(
                        f'Image {idx}: Not enough corners (Left: {left_count}, Right: {right_count}). Skipping.'
                    )
                    continue
                
                # Find common corners
                if charuco_ids_left is not None and charuco_ids_right is not None:
                    left_ids_set = set(charuco_ids_left.flatten())
                    right_ids_set = set(charuco_ids_right.flatten())
                    common_ids = left_ids_set & right_ids_set
                    
                    if len(common_ids) < self.min_corners:
                        self.get_logger().warn(
                            f'Image {idx}: Not enough common corners ({len(common_ids)}). Skipping.'
                        )
                        continue
                    
                    # Filter to keep only common corners
                    common_ids_sorted = sorted(common_ids)
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
                    obj_pts = self.charuco_board.getChessboardCorners()[ids_filtered.flatten()]
                    
                    # Store calibration data
                    self.all_corners_left.append(corners_left_filtered)
                    self.all_corners_right.append(corners_right_filtered)
                    self.all_ids_left.append(ids_filtered)
                    self.all_ids_right.append(ids_filtered)
                    self.obj_points.append(obj_pts)
                    
                    self.images_captured += 1
                    self.get_logger().info(
                        f'[{self.images_captured}/{self.num_images_required}] '
                        f'Loaded image {idx}: {len(common_ids)} common corners.'
                    )
                    
                    # Check if we have enough images
                    if self.images_captured >= self.num_images_required:
                        self.get_logger().info('')
                        self.get_logger().info('All images loaded! Computing calibration...')
                        self.calibration_done = True  # Prevent further captures
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
            self.all_corners_left = []
            self.all_corners_right = []
            self.all_ids_left = []
            self.all_ids_right = []
            self.obj_points = []
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

        # Convert to grayscale for detection
        left_gray = cv2.cvtColor(left_img, cv2.COLOR_BGR2GRAY)
        right_gray = cv2.cvtColor(right_img, cv2.COLOR_BGR2GRAY)
        
        # Debug: check if images are valid (not all black/white)
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
        
        # Debug: log marker detection results
        left_markers = len(marker_ids_left) if marker_ids_left is not None else 0
        right_markers = len(marker_ids_right) if marker_ids_right is not None else 0
        left_corners = len(charuco_ids_left) if charuco_ids_left is not None else 0
        right_corners = len(charuco_ids_right) if charuco_ids_right is not None else 0
        
        self.get_logger().debug(
            f'Detection results - Left: {left_markers} markers, {left_corners} corners | '
            f'Right: {right_markers} markers, {right_corners} corners'
        )
        
        # Additional debug: explain why corners might not be interpolated
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

        # Check if enough corners detected in both images
        left_count = len(charuco_ids_left) if charuco_ids_left is not None else 0
        right_count = len(charuco_ids_right) if charuco_ids_right is not None else 0

        if left_count < self.min_corners or right_count < self.min_corners:
            self.get_logger().warn(
                f'Not enough corners detected! Left: {left_count}, Right: {right_count} '
                f'(need {self.min_corners}+). Try again.'
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
            
            # Save diagnostic images to help debug
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
            
            return

        # Find common corner IDs between left and right
        if charuco_ids_left is not None and charuco_ids_right is not None:
            left_ids_set = set(charuco_ids_left.flatten())
            right_ids_set = set(charuco_ids_right.flatten())
            common_ids = left_ids_set & right_ids_set
            
            if len(common_ids) < self.min_corners:
                self.get_logger().warn(
                    f'Not enough common corners! Common: {len(common_ids)} '
                    f'(need {self.min_corners}+). Try again.'
                )
                return

            # Filter to keep only common corners (in same order)
            common_ids_sorted = sorted(common_ids)
            
            # Create filtered arrays
            left_mask = [i for i, id_val in enumerate(charuco_ids_left.flatten()) 
                        if id_val in common_ids]
            right_mask = [i for i, id_val in enumerate(charuco_ids_right.flatten()) 
                         if id_val in common_ids]
            
            # Sort by ID to ensure matching order
            left_order = np.argsort(charuco_ids_left.flatten()[left_mask])
            right_order = np.argsort(charuco_ids_right.flatten()[right_mask])
            
            corners_left_filtered = charuco_corners_left[left_mask][left_order]
            corners_right_filtered = charuco_corners_right[right_mask][right_order]
            ids_filtered = charuco_ids_left[left_mask][left_order]

            # Get 3D object points for these corner IDs
            obj_pts = self.charuco_board.getChessboardCorners()[ids_filtered.flatten()]

            # Store calibration data
            self.all_corners_left.append(corners_left_filtered)
            self.all_corners_right.append(corners_right_filtered)
            self.all_ids_left.append(ids_filtered)
            self.all_ids_right.append(ids_filtered)
            self.obj_points.append(obj_pts)

            self.images_captured += 1
            
            # Save images to /tmp
            try:
                img_num = self.images_captured
                cv2.imwrite(str(self.tmp_image_dir / f'left_{img_num:03d}.png'), left_img)
                cv2.imwrite(str(self.tmp_image_dir / f'right_{img_num:03d}.png'), right_img)
            except Exception as e:
                self.get_logger().warn(f'Failed to save images to /tmp: {e}')
            
            self.get_logger().info(
                f'[{self.images_captured}/{self.num_images_required}] '
                f'Captured! Detected {len(common_ids)} common corners.'
            )

            # Check if we have enough images
            if self.images_captured >= self.num_images_required:
                self.get_logger().info('')
                self.get_logger().info('All images captured! Computing calibration...')
                self.run_calibration()
        else:
            self.get_logger().warn('ChArUco board not detected in one or both images.')

    def run_calibration(self):
        """Run stereo calibration using collected data."""
        self.calibration_done = True
        
        image_size = (self.image_width, self.image_height)
        
        self.get_logger().info('Running individual camera calibrations...')
        
        # Calibrate left camera
        # Use better initialization: let OpenCV estimate initial camera matrix
        # Don't fix K3 initially - allow full optimization
        ret_left, K1, D1, rvecs_left, tvecs_left = cv2.calibrateCamera(
            self.obj_points,
            self.all_corners_left,
            image_size,
            None, None,
            flags=0  # Allow full optimization of all parameters
        )
        self.get_logger().info(f'Left camera RMS error: {ret_left:.4f}')

        # Calibrate right camera
        # Use better initialization: let OpenCV estimate initial camera matrix
        # Don't fix K3 initially - allow full optimization
        ret_right, K2, D2, rvecs_right, tvecs_right = cv2.calibrateCamera(
            self.obj_points,
            self.all_corners_right,
            image_size,
            None, None,
            flags=0  # Allow full optimization of all parameters
        )
        self.get_logger().info(f'Right camera RMS error: {ret_right:.4f}')

        self.get_logger().info('Running stereo calibration...')
        
        # Stereo calibration
        # Fix intrinsics - individual calibrations should be accurate
        # Only refine extrinsics (R, T) during stereo calibration
        flags = cv2.CALIB_FIX_INTRINSIC
        
        ret_stereo, K1, D1, K2, D2, R, T, E, F = cv2.stereoCalibrate(
            self.obj_points,
            self.all_corners_left,
            self.all_corners_right,
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

        # Store calibration data for saving
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

        # Ask user if they want to save
        self.prompt_save()

    def prompt_save(self):
        """Ask user if they want to save the calibration."""
        print('')
        print('Do you want to save this calibration and replace the existing one?')
        print('Type "y" to save, "n" to discard: ', end='', flush=True)
        
        try:
            response = input().strip().lower()
            if response == 'y':
                try:
                    self.save_calibration()
                except Exception as e:
                    self.get_logger().error(f'Failed to save calibration: {e}')
            else:
                self.get_logger().info('Calibration discarded.')
        except EOFError:
            self.get_logger().info('Calibration discarded (no input).')
        except Exception as e:
            self.get_logger().error(f'Error during save prompt: {e}')
        
        # Signal shutdown
        self.get_logger().info('Calibration complete. Shutting down...')
        try:
            self.destroy_node()
        except Exception as e:
            self.get_logger().warn(f'Error during shutdown: {e}')
        finally:
            # Force exit if shutdown doesn't work
            import sys
            sys.exit(0)

    def save_calibration(self):
        """Save calibration to YAML file."""
        try:
            # Find calibration config directory
            calib_dir = self.find_calibration_dir()
            if calib_dir is None:
                # Create new one
                calib_dir = self.data_directory / 'calibration_config'
                calib_dir.mkdir(parents=True, exist_ok=True)
                self.get_logger().info(f'Created new calibration directory: {calib_dir}')

            output_path = calib_dir / 'stereo_calib.yaml'
            backup_path = calib_dir / 'stereo_calib.yaml.backup'

            # Backup existing file
            if output_path.exists():
                import shutil
                shutil.copy(output_path, backup_path)
                self.get_logger().info(f'Backed up existing calibration to: {backup_path}')

            # Save using OpenCV FileStorage (matching existing format)
            fs = cv2.FileStorage(str(output_path), cv2.FileStorage_WRITE)
            if not fs.isOpened():
                raise RuntimeError(f'Failed to open file for writing: {output_path}')
            
            # Write in same order as existing calibration file
            fs.write('model', 'pinhole')
            fs.write('image_width', self.calibration_data['image_width'])
            fs.write('image_height', self.calibration_data['image_height'])
            fs.write('K1', self.calibration_data['K1'])
            fs.write('D1', self.calibration_data['D1'])
            fs.write('K2', self.calibration_data['K2'])
            fs.write('D2', self.calibration_data['D2'])
            fs.write('R', self.calibration_data['R'])
            fs.write('T', self.calibration_data['T'])
            fs.write('R1', self.calibration_data['R1'])
            fs.write('R2', self.calibration_data['R2'])
            fs.write('P1', self.calibration_data['P1'])
            fs.write('P2', self.calibration_data['P2'])
            fs.write('Q', self.calibration_data['Q'])
            
            fs.release()

            self.get_logger().info(f'Calibration saved to: {output_path}')
            # Flush to ensure message is printed
            import sys
            sys.stdout.flush()
        except Exception as e:
            self.get_logger().error(f'Error saving calibration: {e}')
            raise

    def find_calibration_dir(self):
        """Find existing calibration config directory."""
        if not self.data_directory.exists():
            return None
        
        for entry in self.data_directory.iterdir():
            if entry.is_dir() and 'calibration_config' in entry.name:
                return entry
        
        return None


def main(args=None):
    rclpy.init(args=args)
    
    node = StereoCalibrator()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        node.get_logger().error(f'Error in main: {e}')
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()

