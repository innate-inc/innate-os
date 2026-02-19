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
    ros2 bag record /mars/arm/commands /mars/main_camera/calib/enter_events
"""

import subprocess
import sys
import threading
import time
import json
import math
from dataclasses import dataclass
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from typing import Optional

import cv2
import numpy as np
import rclpy
from maurice_msgs.action import RunStereoCalibration
from maurice_msgs.srv import GotoJS
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import Empty, Float64MultiArray
from std_srvs.srv import Trigger
from cv_bridge import CvBridge
import message_filters

from maurice_cam.calibration_debug_vis import generate_debug_mosaic, generate_visualizations
from maurice_cam.calibration_utils import (
    restore_head_and_arm,
    setup_head_and_arm,
    save_calibration,
    prompt_save,
    find_calibration_dir,
)

# ---------------------------------------------------------------------------
# Stereo calibration arm-control constants
# ---------------------------------------------------------------------------

# "Board handoff" pose:
# all joints at 0, except joint3 tilted +9 degrees upward, gripper open.
PREPARE_BOARD_JOINTS_RAD = [0.0, 0.0, math.radians(9.0), 0.0, 0.0, 0.85]
GRIPPER_OPEN_RAD = 0.85
GRIPPER_CLOSED_RAD = 0.0
PREPARE_MOVE_TIME_SEC = 2.5
GRIPPER_ACTION_TIME_SEC = 1.0
DEFAULT_PREPARE_SERVICE_NAME = "/mars/main_camera/prepare_stereo_calibration"
DEFAULT_STOP_SERVICE_NAME = "/mars/main_camera/stop_stereo_calibration"
DEFAULT_DELETE_SERVICE_NAME = "/mars/main_camera/delete_stereo_calibration"


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
        self.declare_parameter('playback', True)  # Playback mode: replay a rosbag
        _default_bag_path = str(Path(get_package_share_directory('maurice_cam')) / 'calib')
        self.declare_parameter('bag_path', _default_bag_path)     # Path to rosbag for playback
        self.declare_parameter('playback_rate', 2)
        self.declare_parameter('interactive', True)  # CLI mode (stdin prompts)
        self.declare_parameter('auto_start', True)  # Start capture/playback at boot of this node
        self.declare_parameter('run_action_name', '/mars/main_camera/run_stereo_calibration')
        self.declare_parameter('prepare_service_name', DEFAULT_PREPARE_SERVICE_NAME)
        self.declare_parameter('stop_service_name', DEFAULT_STOP_SERVICE_NAME)
        self.declare_parameter('delete_service_name', DEFAULT_DELETE_SERVICE_NAME)
        self.declare_parameter('prepare_move_time_sec', PREPARE_MOVE_TIME_SEC)
        self.declare_parameter('gripper_action_time_sec', GRIPPER_ACTION_TIME_SEC)

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
        self.playback = self.get_parameter('playback').value
        self.bag_path = self.get_parameter('bag_path').value
        self.playback_rate = float(self.get_parameter('playback_rate').value)
        self.interactive = bool(self.get_parameter('interactive').value)
        self.auto_start = bool(self.get_parameter('auto_start').value)
        self.run_action_name = str(self.get_parameter('run_action_name').value)
        self.prepare_service_name = str(
            self.get_parameter('prepare_service_name').value
        )
        self.stop_service_name = str(self.get_parameter('stop_service_name').value)
        self.delete_service_name = str(self.get_parameter('delete_service_name').value)
        self.prepare_move_time_sec = float(
            self.get_parameter('prepare_move_time_sec').value
        )
        self.gripper_action_time_sec = float(
            self.get_parameter('gripper_action_time_sec').value
        )

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
        self.images_captured = 0
        self.capture_attempts = 0
        self.calibration_done = False
        self.calibration_data = None
        self._capture_enabled = self.interactive
        self._cancel_requested = False
        self._active_goal = None
        self._active_goal_lock = threading.Lock()
        self._playback_proc = None
        self._playback_proc_lock = threading.Lock()
        self._last_output_path = ""
        self._last_rms = {"left": 0.0, "right": 0.0, "stereo": 0.0}

        # Re-entrant callback group so services/cancel callbacks can run while
        # the long-running action execute callback is active.
        self._control_cb_group = ReentrantCallbackGroup()

        # Image storage directory - inside data directory so images persist
        self.tmp_image_dir = self.data_directory / 'stereo_calibration_images'
        self.tmp_image_dir.mkdir(parents=True, exist_ok=True)

        # Enter-event topic: keyboard thread publishes, callback triggers capture
        self._enter_pub = self.create_publisher(Empty, '/mars/main_camera/calib/enter_events', 10)
        self._enter_sub = self.create_subscription(
            Empty, '/mars/main_camera/calib/enter_events', self._enter_event_callback, 1
        )

        # Action server for app/API control (single long-running calibration goal)
        self._run_action_server = ActionServer(
            self,
            RunStereoCalibration,
            self.run_action_name,
            execute_callback=self._execute_run_calibration,
            goal_callback=self._goal_callback,
            cancel_callback=self._cancel_callback,
            callback_group=self._control_cb_group,
        )

        # Arm control client used by prepare/start calibration flows.
        self._arm_goto_client = self.create_client(
            GotoJS,
            '/mars/arm/goto_js',
            callback_group=self._control_cb_group,
        )

        # Service for app step 1: move arm to "insert board" pose.
        self._prepare_service = self.create_service(
            Trigger,
            self.prepare_service_name,
            self._prepare_board_service_callback,
            callback_group=self._control_cb_group,
        )

        # Optional convenience service for stop button in app.
        self._stop_service = self.create_service(
            Trigger,
            self.stop_service_name,
            self._stop_calibration_service_callback,
            callback_group=self._control_cb_group,
        )

        # Service to delete existing calibration file.
        self._delete_service = self.create_service(
            Trigger,
            self.delete_service_name,
            self._delete_calibration_service_callback,
            callback_group=self._control_cb_group,
        )

        if self.interactive and self.auto_start:
            # Set up head and arm for interactive calibration.
            setup_head_and_arm(self)

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

        # Start input / playback only in CLI interactive mode.
        if self.interactive and self.auto_start:
            if self.playback:
                self.get_logger().info('Mode: PLAYBACK (replaying rosbag)')
                self._playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
                self._playback_thread.start()
            else:
                self.get_logger().info('Mode: RECORD (manual capture)')
                self.input_thread = threading.Thread(target=self.keyboard_input_loop, daemon=True)
                self.input_thread.start()
                self.get_logger().info('=' * 60)
                self.get_logger().warn('>>> Move the board and press ENTER to capture an image <<<')
        else:
            self._capture_enabled = False
            self.get_logger().info(
                f"Mode: MANAGED (waiting for action goals on '{self.run_action_name}')"
            )
            self.get_logger().info(
                f"Prepare service available on '{self.prepare_service_name}'"
            )
            self.get_logger().info(
                f"Stop service available on '{self.stop_service_name}'"
            )
            self.get_logger().info(
                f"Delete service available on '{self.delete_service_name}'"
            )

    def _reset_calibration_session(self):
        """Reset all capture/calibration buffers for a new run."""
        self.indiv_corners_left = []
        self.indiv_corners_right = []
        self.indiv_obj_points_left = []
        self.indiv_obj_points_right = []
        self.common_corners_left = []
        self.common_corners_right = []
        self.common_obj_points = []
        self.images_captured = 0
        self.capture_attempts = 0
        self.calibration_done = False
        self.calibration_data = None
        self._last_output_path = ""
        self._last_rms = {"left": 0.0, "right": 0.0, "stereo": 0.0}
        with self.frame_lock:
            self.latest_left_frame = None
            self.latest_right_frame = None

    def _goal_callback(self, goal_request):
        """Accept only one active calibration goal at a time."""
        with self._active_goal_lock:
            if self._active_goal is not None:
                self.get_logger().warn("Rejecting calibration goal: another run is in progress")
                return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _cancel_callback(self, goal_handle):
        """Allow cancellation of the current run."""
        del goal_handle
        self._request_stop_active_run("Calibration cancel requested")
        return CancelResponse.ACCEPT

    def _publish_action_feedback(self, goal_handle, phase: str, message: str):
        feedback = RunStereoCalibration.Feedback()
        feedback.phase = phase
        feedback.images_captured = int(self.images_captured)
        feedback.capture_attempts = int(self.capture_attempts)
        feedback_payload = {
            "phase": phase,
            "images_captured": int(self.images_captured),
            "capture_attempts": int(self.capture_attempts),
            "message": message,
        }
        feedback.message = json.dumps(feedback_payload, separators=(",", ":"), ensure_ascii=True)
        goal_handle.publish_feedback(feedback)

    def _terminate_playback_process(self) -> bool:
        """Terminate active rosbag playback process if it is running."""
        with self._playback_proc_lock:
            proc = self._playback_proc

        if proc is None or proc.poll() is not None:
            return False

        try:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except Exception:
                proc.kill()
            return True
        except Exception as e:
            self.get_logger().warn(f"Failed to terminate playback process: {e}")
            return False

    def _request_stop_active_run(self, reason: str):
        """Request stop for active run and terminate playback promptly."""
        self._cancel_requested = True
        terminated = self._terminate_playback_process()
        if terminated:
            self.get_logger().info(f"{reason}; playback process terminated")
        else:
            self.get_logger().info(f"{reason}; cancellation flag set")

    def _call_arm_goto_js(
        self,
        joint_positions: list[float],
        duration_sec: float,
    ) -> tuple[bool, str]:
        """Call /mars/arm/goto_js with a 6-DOF target."""
        if len(joint_positions) != 6:
            return False, f"Expected 6 joints, got {len(joint_positions)}"

        if not self._arm_goto_client.wait_for_service(timeout_sec=2.0):
            return False, "/mars/arm/goto_js service not available"

        req = GotoJS.Request()
        req.data = Float64MultiArray()
        req.data.data = [float(v) for v in joint_positions]
        req.time = float(duration_sec)

        try:
            future = self._arm_goto_client.call_async(req)
        except Exception as e:
            return False, f"Failed to call /mars/arm/goto_js: {e}"

        timeout_sec = max(2.0, float(duration_sec) + 5.0)
        end_time = time.time() + timeout_sec
        while rclpy.ok() and not future.done() and time.time() < end_time:
            time.sleep(0.05)

        if not future.done():
            return False, f"/mars/arm/goto_js timed out after {timeout_sec:.1f}s"

        try:
            result = future.result()
        except Exception as e:
            return False, f"/mars/arm/goto_js failed: {e}"

        if result is None:
            return False, "/mars/arm/goto_js returned no result"
        if not result.success:
            return False, "/mars/arm/goto_js reported failure"
        return True, "ok"

    def _move_arm_to_prepare_pose(self, gripper_open: bool) -> tuple[bool, str]:
        """Move arm to board handoff pose with open/closed gripper."""
        target = list(PREPARE_BOARD_JOINTS_RAD)
        target[5] = GRIPPER_OPEN_RAD if gripper_open else GRIPPER_CLOSED_RAD
        duration = (
            self.prepare_move_time_sec if gripper_open else self.gripper_action_time_sec
        )
        success, message = self._call_arm_goto_js(target, duration)
        if success:
            grip_state = "open" if gripper_open else "closed"
            self.get_logger().info(
                f"Arm moved to calibration handoff pose (gripper {grip_state})"
            )
        return success, message

    def _prepare_board_service_callback(self, request, response):
        """Service callback to place arm in board insertion pose."""
        del request

        with self._active_goal_lock:
            if self._active_goal is not None:
                response.success = False
                response.message = "Calibration is running; stop it before prepare."
                return response

        success, message = self._move_arm_to_prepare_pose(gripper_open=True)
        response.success = bool(success)
        if success:
            response.message = (
                "Arm ready for board insertion "
                "(joint3 +9deg, gripper open)."
            )
        else:
            response.message = f"Failed to move to board insertion pose: {message}"
        return response

    def _stop_calibration_service_callback(self, request, response):
        """Service callback for stop button while action is running."""
        del request

        with self._active_goal_lock:
            active = self._active_goal is not None

        if not active:
            response.success = True
            response.message = "No active calibration run."
            return response

        self._request_stop_active_run("Stop service requested")
        response.success = True
        response.message = "Stop requested."
        return response

    def _delete_calibration_service_callback(self, request, response):
        """Service callback to delete existing calibration file."""
        del request

        with self._active_goal_lock:
            if self._active_goal is not None:
                response.success = False
                response.message = "Cannot delete calibration while calibration is running."
                return response

        # Find calibration directory using same logic as save_calibration
        calib_dir = find_calibration_dir(self)
        if calib_dir is None:
            response.success = False
            response.message = "No calibration directory found"
            return response

        # Use same filename as save_calibration
        calib_file = calib_dir / 'stereo_calib.yaml'
        
        if not calib_file.exists():
            response.success = False
            response.message = f"Calibration file not found: {calib_file}"
            return response

        try:
            # Rename to backup instead of deleting
            timestamp = int(time.time())
            backup_file = calib_file.parent / f"{calib_file.stem}.backup.{timestamp}{calib_file.suffix}"
            calib_file.rename(backup_file)
            self.get_logger().info(f"Backed up calibration file: {calib_file} -> {backup_file}")
            response.success = True
            response.message = f"Successfully backed up calibration file to: {backup_file}"
        except Exception as e:
            self.get_logger().error(f"Failed to backup calibration file: {e}")
            response.success = False
            response.message = f"Failed to backup calibration file: {e}"
        
        return response

    def _execute_run_calibration(self, goal_handle):
        """Execute a managed stereo calibration run for app/API clients."""
        with self._active_goal_lock:
            self._active_goal = goal_handle

        try:
            goal = goal_handle.request
            self._cancel_requested = False
            self._reset_calibration_session()
            self._capture_enabled = True

            # Allow per-goal overrides while keeping sane defaults.
            if goal.num_images > 0:
                self.num_images_required = int(goal.num_images)
            if goal.min_corners > 0:
                self.min_corners = int(goal.min_corners)

            if not goal.use_playback:
                msg = "Manual capture mode is not implemented in managed action yet"
                self.get_logger().error(msg)
                self._capture_enabled = False
                goal_handle.abort()
                result = RunStereoCalibration.Result()
                result.success = False
                result.message = msg
                return result

            bag_path = goal.bag_path.strip() if goal.bag_path.strip() else self.bag_path
            playback_rate = float(goal.playback_rate) if goal.playback_rate > 0.0 else self.playback_rate

            self._publish_action_feedback(
                goal_handle,
                "INIT",
                f"Starting playback from '{bag_path}' at {playback_rate:.2f}x",
            )

            setup_head_and_arm(self)

            playback_ok = self._playback_loop(
                bag_path_override=bag_path,
                playback_rate=playback_rate,
                auto_finalize=False,
                goal_handle=goal_handle,
            )

            if self._cancel_requested or goal_handle.is_cancel_requested:
                restore_head_and_arm(self)
                self._capture_enabled = False
                goal_handle.canceled()
                result = RunStereoCalibration.Result()
                result.success = False
                result.message = "Calibration cancelled"
                result.images_captured = int(self.images_captured)
                return result

            if not playback_ok:
                restore_head_and_arm(self)
                self._capture_enabled = False
                msg = "Playback failed"
                self.get_logger().error(msg)
                goal_handle.abort()
                result = RunStereoCalibration.Result()
                result.success = False
                result.message = msg
                result.images_captured = int(self.images_captured)
                return result

            if self.images_captured <= 0:
                self._capture_enabled = False
                msg = "No captures were recorded during playback"
                self.get_logger().error(msg)
                goal_handle.abort()
                result = RunStereoCalibration.Result()
                result.success = False
                result.message = msg
                return result

            self._publish_action_feedback(
                goal_handle,
                "CALIBRATING",
                f"Captured {self.images_captured} image pairs, running calibration",
            )

            success, message = self.run_calibration(
                save_decision=bool(goal.save_calibration),
                shutdown_on_complete=False,
            )
            self._capture_enabled = False

            result = RunStereoCalibration.Result()
            result.success = bool(success)
            result.message = message
            result.images_captured = int(self.images_captured)
            result.left_rms = float(self._last_rms["left"])
            result.right_rms = float(self._last_rms["right"])
            result.stereo_rms = float(self._last_rms["stereo"])
            result.output_path = self._last_output_path

            if success:
                goal_handle.succeed()
            else:
                goal_handle.abort()
            return result

        except Exception as e:
            self._capture_enabled = False
            self.get_logger().error(f"Managed calibration failed: {e}")
            goal_handle.abort()
            result = RunStereoCalibration.Result()
            result.success = False
            result.message = f"Calibration failed: {e}"
            result.images_captured = int(self.images_captured)
            return result
        finally:
            with self._active_goal_lock:
                self._active_goal = None

    def image_callback(self, left_msg, right_msg):
        """Store latest left and right frames."""
        if not self._capture_enabled:
            return
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

    # ------------------------------------------------------------------ #
    #  Enter-event handling
    # ------------------------------------------------------------------ #

    def _enter_event_callback(self, msg: Empty):
        """Called when an enter event is received — grab latest frames and process."""
        if self.calibration_done or not self._capture_enabled:
            return

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
                f'[{self.images_captured}] '
                f'Captured! Detected {result.num_common} common corners.'
            )

        # Generate debug mosaic after every capture attempt
        if self.debug:
            generate_debug_mosaic(self, result)

    # ------------------------------------------------------------------ #
    #  Rosbag playback
    # ------------------------------------------------------------------ #

    def _playback_loop(
        self,
        bag_path_override: Optional[str] = None,
        playback_rate: Optional[float] = None,
        auto_finalize: bool = True,
        goal_handle=None,
    ) -> bool:
        """Play rosbag and optionally finalize calibration when playback finishes."""
        bag = bag_path_override if bag_path_override else self.bag_path
        if not bag:
            bag = str(self.tmp_image_dir / 'calibration_bag')

        bag_path = Path(bag)
        if not bag_path.exists():
            self.get_logger().error(f'Bag not found: {bag_path}')
            return False

        rate = playback_rate if playback_rate and playback_rate > 0.0 else self.playback_rate
        self.get_logger().info(f'Playing rosbag: {bag_path} at {rate:.2f}x')

        # Wait a moment for subscriptions to connect
        time.sleep(2.0)

        proc = None
        try:
            proc = subprocess.Popen(
                ['ros2', 'bag', 'play', '-r', str(rate), str(bag_path)],
            )
            with self._playback_proc_lock:
                self._playback_proc = proc

            last_feedback_time = 0.0
            while True:
                if self._cancel_requested:
                    self._terminate_playback_process()
                    self.get_logger().info('Playback terminated due to cancellation')
                    return False

                if goal_handle is not None:
                    now = time.time()
                    if now - last_feedback_time >= 0.5:
                        self._publish_action_feedback(
                            goal_handle,
                            "PLAYBACK",
                            f"Captures: {self.images_captured}/{self.num_images_required}",
                        )
                        last_feedback_time = now

                rc = proc.poll()
                if rc is not None:
                    if rc != 0:
                        self.get_logger().warn(f'ros2 bag play exited with code {rc}')
                        return False
                    break
                time.sleep(0.1)

        except Exception as e:
            self.get_logger().error(f'Failed to play bag: {e}')
            return False
        finally:
            with self._playback_proc_lock:
                if proc is not None and self._playback_proc is proc:
                    self._playback_proc = None

        self.get_logger().info(
            f'Bag playback finished — captured {self.images_captured} images'
        )

        if auto_finalize:
            if self.images_captured > 0 and not self.calibration_done:
                self.get_logger().info('Running calibration with captured images...')
                self.run_calibration()
            elif self.images_captured == 0:
                self.get_logger().error('No images captured during playback. Check your bag.')

        return True

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
                input(f'[{self.images_captured} captured] Move the board and press Enter (Ctrl+C to finish and calibrate)')
                if not self.calibration_done:
                    self._enter_pub.publish(Empty())
            except (EOFError, KeyboardInterrupt):
                break

    def _process_image_pair(self, left_img, right_img, label='capture', save_images=False) -> DetectionResult:
        """Detect ChArUco corners in a stereo image pair and store if valid.

        This is the shared detection/filtering/storage pipeline used by both
        live capture and offline reload (``load_existing_images``).

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
            except Exception as e:
                self.get_logger().warn(f'Failed to save images: {e}')

        result.success = True
        result.num_common = len(common_ids)
        result.common_ids = common_ids
        result.corners_left_filtered = corners_left_filtered
        result.corners_right_filtered = corners_right_filtered
        result.obj_pts_common = obj_pts_common
        return result

    def run_calibration(
        self,
        save_decision: Optional[bool] = None,
        shutdown_on_complete: bool = True,
    ) -> tuple[bool, str]:
        """Run stereo calibration using collected data.

        Args:
            save_decision:
                - None: keep interactive prompt behavior (CLI mode)
                - True: save calibration without prompt
                - False: discard calibration without prompt
            shutdown_on_complete:
                Whether to shutdown the ROS context at the end of a non-interactive
                run. Interactive mode delegates shutdown to prompt_save().

        Returns:
            tuple(success, message)
        """
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
        self._last_rms = {
            "left": float(ret_left),
            "right": float(ret_right),
            "stereo": float(ret_stereo),
        }

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
            'version': 2,
        }

        # Generate visualization images
        self.get_logger().info('Generating visualization images...')
        generate_visualizations(self)

        # Interactive CLI mode: ask user and terminate through prompt_save().
        if save_decision is None:
            prompt_save(self)
            return True, "Calibration complete"

        # Managed mode: save/discard without stdin interaction.
        try:
            if save_decision:
                output_path = save_calibration(self)
                self._last_output_path = output_path or ""
                msg = f"Calibration saved to {self._last_output_path}"
            else:
                msg = "Calibration computed and discarded"
                self.get_logger().info(msg)
            restore_head_and_arm(self)
            if shutdown_on_complete and rclpy.ok():
                rclpy.shutdown()
            return True, msg
        except Exception as e:
            restore_head_and_arm(self)
            return False, f"Failed to finalize calibration: {e}"



def main(args=None):
    rclpy.init(args=args)
    
    node = StereoCalibrator()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    
    try:
        executor.spin()
    except KeyboardInterrupt:
        if not node.calibration_done and node.images_captured > 0:
            print(f'\n\nCtrl+C received — running calibration with {node.images_captured} captured images...\n')
            node.run_calibration()
        elif node.images_captured == 0:
            print('\nCtrl+C received — no images captured, exiting.')
    except Exception as e:
        # "context is not valid" is expected on SIGTERM / normal shutdown — ignore it
        if 'context is not valid' not in str(e):
            print(f'[stereo_calibrator] Error in main: {e}', file=sys.stderr)

    try:
        executor.shutdown()
    except Exception:
        pass

    try:
        if hasattr(node, "_run_action_server") and node._run_action_server is not None:
            node._run_action_server.destroy()
    except Exception:
        pass

    try:
        node.destroy_node()
    except Exception:
        pass
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == '__main__':
    main()
