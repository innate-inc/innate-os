#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Stereo Camera Calibration Node — ChArUco (production) or plain checkerboard (experiment).

This launches an interactive calibration tool that:
1. Subscribes to the stereo camera topic
2. Allows user to capture images by pressing Enter
3. Detects target corners in both cameras
4. Performs stereo calibration after collecting enough images
5. Optionally saves the calibration to replace the existing one

Both target types share one OpenCV pinhole pipeline: independent
``calibrateCamera`` per eye, then ``stereoCalibrate`` with ``CALIB_FIX_INTRINSIC``.
Only the detector and the bookkeeping around it differ, so the two arms are
directly comparable.

``target_type:=checkerboard`` additionally holds out every Nth accepted pair,
scores the solve on those unseen images, and writes everything to a separate
experiment directory — it never touches the production ``stereo_calib.yaml``.

Usage:
    ros2 run mars_cam stereo_calibrator
    or
    ros2 run mars_cam stereo_calibrator --ros-args -p squares_y:=8 -p squares_x:=11 -p square_size:=0.016 -p marker_size:=0.012
    or
    ros2 run mars_cam stereo_calibrator --ros-args -p target_type:=checkerboard

    Then press Enter to capture images. After 30 images, calibration is computed.
    ros2 bag record /mars/main_camera/calib/enter_events
"""

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import message_filters
import numpy as np
import rclpy
from cv_bridge import CvBridge
from mars_msgs.action import RunStereoCalibration
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage, Image
from std_msgs.msg import Bool
from std_srvs.srv import Trigger

from mars_cam import capture_guidance as guidance
from mars_cam import checkerboard as cb
from mars_cam.calibration_debug_vis import generate_coverage_images, generate_debug_mosaic, generate_visualizations
from mars_cam.calibration_experiment import ExperimentRecorder, skew_summary
from mars_cam.calibration_utils import (
    find_calibration_dir,
    prompt_save,
    restore_head,
    save_calibration,
    setup_head,
)
from mars_cam.calibration_validation import (
    StereoCalibrationMatrices,
    ValidationPair,
    evaluate,
)

DEFAULT_STOP_SERVICE_NAME = "/mars/main_camera/stop_stereo_calibration"
DEFAULT_DELETE_SERVICE_NAME = "/mars/main_camera/delete_stereo_calibration"

TARGET_CHARUCO = "charuco"
TARGET_CHECKERBOARD = "checkerboard"

# Goal.board -> target_type. BOARD_DEFAULT leaves the node's configured target
# alone, so a CLI run and an app run with no explicit choice behave the same.
BOARD_BY_GOAL = {
    RunStereoCalibration.Goal.BOARD_CHARUCO: TARGET_CHARUCO,
    RunStereoCalibration.Goal.BOARD_CHECKERBOARD: TARGET_CHECKERBOARD,
}


def _stamp_sec(msg: Image) -> float:
    return float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) * 1e-9


@dataclass
class DetectionResult:
    """Result of ChArUco detection on a stereo image pair."""

    success: bool = False
    num_common: int = 0

    # Input images (passed through for debug mosaic)
    left_img: np.ndarray | None = None
    right_img: np.ndarray | None = None

    # ArUco marker detections
    marker_corners_left: tuple | None = None
    marker_ids_left: np.ndarray | None = None
    marker_corners_right: tuple | None = None
    marker_ids_right: np.ndarray | None = None

    # ChArUco corner detections
    charuco_corners_left: np.ndarray | None = None
    charuco_ids_left: np.ndarray | None = None
    charuco_corners_right: np.ndarray | None = None
    charuco_ids_right: np.ndarray | None = None

    # Filtered common corners & 3D object points (for stereoCalibrate)
    common_ids: set | None = None
    corners_left_filtered: np.ndarray | None = None
    corners_right_filtered: np.ndarray | None = None
    obj_pts_common: np.ndarray | None = None


class StereoCalibrator(Node):
    """ROS2 node for interactive stereo camera calibration using ChArUco boards."""

    def __init__(self):
        super().__init__("stereo_calibrator")

        # Declare parameters
        self.declare_parameter("left_topic", "/mars/main_camera/left/image_raw")
        self.declare_parameter("right_topic", "/mars/main_camera/right/image_raw")
        self.declare_parameter("image_width", 640)
        self.declare_parameter("image_height", 480)
        self.declare_parameter("data_directory", "/home/jetson1/innate-os/data")

        # Target selection: "charuco" (production) or "checkerboard" (A/B experiment)
        self.declare_parameter("target_type", TARGET_CHARUCO)

        # ChArUco board parameters (calib.io 17x9 squares -> 16x8 = 128 interpolated corners)
        self.declare_parameter("squares_x", 17)  # squares across, 272mm at 16mm pitch
        self.declare_parameter("squares_y", 9)  # squares down, 144mm at 16mm pitch
        self.declare_parameter("square_size", 0.016)  # 16mm in meters
        self.declare_parameter("marker_size", 0.012)  # 12mm in meters
        self.declare_parameter("dictionary_id", cv2.aruco.DICT_4X4_250)

        # Plain-checkerboard parameters (US-Letter target: 10x7 squares -> 9x6 inner corners).
        # Measure the printed pitch and override checkerboard_square_size if the
        # printer scaled the page — every metric below is reported against it.
        self.declare_parameter("checkerboard_cols", 9)  # inner corners in X
        self.declare_parameter("checkerboard_rows", 6)  # inner corners in Y
        self.declare_parameter("checkerboard_square_size", 0.022)  # 22mm in meters
        self.declare_parameter("checkerboard_num_images", 40)
        # Every Nth accepted pair is held out of the solve; 5 -> 20%. 0 disables.
        self.declare_parameter("validation_stride", 5)
        self.declare_parameter("validate_charuco", False)
        self.declare_parameter("experiment_directory", "")

        # Calibration parameters
        self.declare_parameter("num_images", 20)
        self.declare_parameter(
            "min_corners", 10
        )  # Minimum corners to accept an image (recommend 10+ for calibrateCamera)
        self.declare_parameter("use_legacy_pattern", True)  # Enable for calib.io boards (OpenCV 4.6.0+)
        self.declare_parameter("debug", False)  # Enable debug mosaic after each capture
        self.declare_parameter("interactive", True)  # CLI mode (stdin prompts)
        self.declare_parameter("auto_start", True)  # Start capture at boot of this node (CLI mode only)
        self.declare_parameter("run_action_name", "/mars/main_camera/run_stereo_calibration")
        self.declare_parameter("stop_service_name", DEFAULT_STOP_SERVICE_NAME)
        self.declare_parameter("delete_service_name", DEFAULT_DELETE_SERVICE_NAME)
        # How long to wait for a capture_trigger before timing out a managed run
        # (guards against an orphaned goal if the requesting client disconnects).
        self.declare_parameter("capture_timeout_sec", 60.0)
        # Stereo pair sync tolerance. The driver splits one side-by-side capture,
        # so both eyes carry the same stamp and 146 measured captures showed a
        # median and p95 skew of 0.0ms. This sits below the 33ms frame period so
        # adjacent frames can never be paired; loosen it only for a driver that
        # genuinely stamps the eyes separately.
        self.declare_parameter("sync_slop_sec", 0.01)

        # Get parameters
        self.left_topic = self.get_parameter("left_topic").value
        self.right_topic = self.get_parameter("right_topic").value
        self.image_width = self.get_parameter("image_width").value
        self.image_height = self.get_parameter("image_height").value
        self.data_directory = Path(self.get_parameter("data_directory").value)

        self.target_type = str(self.get_parameter("target_type").value).strip().lower()
        if self.target_type not in (TARGET_CHARUCO, TARGET_CHECKERBOARD):
            raise ValueError(
                f"target_type must be '{TARGET_CHARUCO}' or '{TARGET_CHECKERBOARD}', got {self.target_type!r}"
            )

        self.squares_x = self.get_parameter("squares_x").value
        self.squares_y = self.get_parameter("squares_y").value
        self.square_size = self.get_parameter("square_size").value
        self.marker_size = self.get_parameter("marker_size").value
        self.dictionary_id = self.get_parameter("dictionary_id").value

        self.checkerboard = cb.CheckerboardTarget(
            pattern=(
                int(self.get_parameter("checkerboard_cols").value),
                int(self.get_parameter("checkerboard_rows").value),
            ),
            square_size=float(self.get_parameter("checkerboard_square_size").value),
        )
        self.sync_slop_sec = float(self.get_parameter("sync_slop_sec").value)
        self.validation_stride = int(self.get_parameter("validation_stride").value)
        self.validate_charuco = bool(self.get_parameter("validate_charuco").value)

        self.num_images_required = self.get_parameter("num_images").value
        if self.target_type == TARGET_CHECKERBOARD:
            self.num_images_required = int(self.get_parameter("checkerboard_num_images").value)
        self.min_corners = self.get_parameter("min_corners").value
        self.use_legacy_pattern = self.get_parameter("use_legacy_pattern").value
        self.debug = self.get_parameter("debug").value
        self.interactive = bool(self.get_parameter("interactive").value)
        self.auto_start = bool(self.get_parameter("auto_start").value)
        self.run_action_name = str(self.get_parameter("run_action_name").value)
        self.stop_service_name = str(self.get_parameter("stop_service_name").value)
        self.delete_service_name = str(self.get_parameter("delete_service_name").value)

        # Create ChArUco board
        self.aruco_dict = cv2.aruco.getPredefinedDictionary(self.dictionary_id)
        self.charuco_board = cv2.aruco.CharucoBoard(
            (self.squares_x, self.squares_y), self.square_size, self.marker_size, self.aruco_dict
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
        self.latest_stamps: tuple[float, float] = (0.0, 0.0)
        self.frame_lock = threading.Lock()
        # Live capture guidance. Built here as well as per-run, because CLI mode
        # detects without ever opening an action goal.
        self._coverage = guidance.CoverageTracker(self.num_images_required)
        self._last_view: guidance.BoardView | None = None
        self.images_captured = 0
        self.capture_attempts = 0
        self.calibration_done = False
        self.calibration_data = None
        self._capture_enabled = self.interactive
        self._cancel_requested = False
        self._active_goal = None
        self._active_goal_lock = threading.Lock()
        self._last_rms = {"left": 0.0, "right": 0.0, "stereo": 0.0}
        self._last_quality = ""
        # Watchdog: aborts a managed run if no capture_trigger arrives in time
        # (guards against an orphaned goal when the requesting client disconnects).
        self._last_capture_time = 0.0
        self._watchdog_active = False
        self._watchdog_timed_out = False

        # Re-entrant callback group so services/cancel callbacks can run while
        # the long-running action execute callback is active.
        self._control_cb_group = ReentrantCallbackGroup()

        # Image storage directory - inside data directory so images persist
        self.tmp_image_dir = self.data_directory / "stereo_calibration_images"
        self.tmp_image_dir.mkdir(parents=True, exist_ok=True)

        # Experiment bookkeeping. The recorder owns a run directory that is
        # deliberately outside the production calibration directory.
        self.capture_diagnostics: list[cb.CaptureDiagnostics] = []
        self.validation_indices: set[int] = set()
        self.recorder: ExperimentRecorder | None = None
        # A held-out split with nowhere to report it would silently shrink the
        # training set and produce no metrics, so the recorder follows the split.
        if self.target_type == TARGET_CHECKERBOARD or self.validate_charuco:
            configured_root = str(self.get_parameter("experiment_directory").value).strip()
            root = Path(configured_root) if configured_root else self.data_directory / "calibration_experiments"
            self.recorder = ExperimentRecorder(root, self.target_type)
            self.tmp_image_dir = self.recorder.image_dir
            self.get_logger().info(f"{self.target_type} experiment output: {self.recorder.run_dir}")

        # Enter-event topic: keyboard thread publishes, callback triggers capture
        self._enter_pub = self.create_publisher(Bool, "/mars/main_camera/calib/enter_events", 10)
        self._enter_sub = self.create_subscription(
            Bool, "/mars/main_camera/calib/enter_events", self._enter_event_callback, 1
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

        # Watchdog timer: created once and left running for the node's lifetime,
        # gated by self._watchdog_active rather than destroyed/recreated per goal
        # (destroying timers/subscriptions from under a spinning executor has caused
        # crash-loops elsewhere in this codebase).
        self._watchdog_timer = self.create_timer(
            1.0, self._check_capture_timeout, callback_group=self._control_cb_group
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
            setup_head(self)

            # Check for existing images and ask user
            self.check_existing_images()

        # Print info (one concise summary line; full detail at debug)
        if self.target_type == TARGET_CHECKERBOARD:
            cols, rows = self.checkerboard.pattern
            target_summary = (
                f"checkerboard {cols}x{rows} inner corners, {self.checkerboard.square_size * 1000:.1f}mm pitch"
            )
        else:
            target_summary = f"ChArUco {self.squares_x}x{self.squares_y}"
        self.get_logger().info(
            f"Stereo Camera Calibrator ready ({target_summary}, {self.num_images_required} images required)"
        )
        self.get_logger().debug("=" * 60)
        self.get_logger().debug(f"Left topic: {self.left_topic}")
        self.get_logger().debug(f"Right topic: {self.right_topic}")
        self.get_logger().debug(f"Image size: {self.image_width}x{self.image_height} per camera")
        self.get_logger().debug(
            f"Square size: {self.square_size * 1000:.1f}mm, Marker size: {self.marker_size * 1000:.1f}mm"
        )
        if self.use_legacy_pattern:
            self.get_logger().debug("Legacy pattern enabled (for calib.io boards)")

        # Create synchronized subscriptions for left and right images
        self.left_sub = message_filters.Subscriber(self, Image, self.left_topic)
        self.right_sub = message_filters.Subscriber(self, Image, self.right_topic)

        # Use ApproximateTimeSynchronizer since timestamps may differ slightly
        self.sync = message_filters.ApproximateTimeSynchronizer(
            [self.left_sub, self.right_sub],
            queue_size=10,
            slop=self.sync_slop_sec,
        )
        self.sync.registerCallback(self.image_callback)

        # Start keyboard input only in CLI interactive mode.
        if self.interactive and self.auto_start:
            self.get_logger().info("Mode: RECORD (manual capture)")
            self.input_thread = threading.Thread(target=self.keyboard_input_loop, daemon=True)
            self.input_thread.start()
            self.get_logger().info("=" * 60)
            self.get_logger().warn(">>> Move the board and press ENTER to capture an image <<<")
        else:
            self._capture_enabled = False
            self.get_logger().info(f"Mode: MANAGED (waiting for action goals on '{self.run_action_name}')")
            self.get_logger().info(f"Stop service available on '{self.stop_service_name}'")
            self.get_logger().info(f"Delete service available on '{self.delete_service_name}'")

    def _note_view(self, image_points, board_points) -> None:
        """Measure what the operator is holding, for the live distance hint.

        Measured from the LEFT image only. The two eyes see the board at almost
        the same size and tilt, and a hint that flickered between them would
        read as noise rather than as advice. Called on rejected attempts too —
        "too far" is most useful precisely when detection just failed.
        """
        if image_points is None or board_points is None:
            self._last_view = None
            return
        shape = self.latest_left_frame.shape if self.latest_left_frame is not None else None
        frame_size = (shape[1], shape[0]) if shape else (self.image_width, self.image_height)
        self._last_view = guidance.measure(image_points, board_points, frame_size)

    def _count_toward_coverage(self) -> None:
        """Fold the last measured view into coverage, once a capture is kept."""
        if self._last_view is not None:
            self._coverage.add(self._last_view)

    def _rejection_advice(self) -> str:
        """Why a capture was dropped, in terms the operator can act on.

        A board that was seen but sits outside the usable size range gets the
        specific reason; "no board detected" is only the honest answer when
        nothing was found at all.
        """
        view = self._last_view
        if view is None:
            return "No board detected — is the whole board in both cameras?"
        if view.hint == guidance.TOO_FAR:
            return "Board too far away — move it closer to the cameras"
        if view.hint == guidance.TOO_CLOSE:
            return "Board too close — back it off so the whole board stays in frame"
        return "Board partly visible — every corner must be in both cameras"

    def _reset_calibration_session(self):
        """Reset all capture/calibration buffers for a new run."""
        self.indiv_corners_left = []
        self.indiv_corners_right = []
        self.indiv_obj_points_left = []
        self.indiv_obj_points_right = []
        self.common_corners_left = []
        self.common_corners_right = []
        self.common_obj_points = []
        self.capture_diagnostics = []
        self.validation_indices = set()
        self.images_captured = 0
        self.capture_attempts = 0
        self.calibration_done = False
        self.calibration_data = None
        self._last_rms = {"left": 0.0, "right": 0.0, "stereo": 0.0}
        self._last_quality = ""
        self._coverage = guidance.CoverageTracker(self.num_images_required)
        self._last_view = None
        with self.frame_lock:
            self.latest_left_frame = None
            self.latest_right_frame = None
            self.latest_stamps = (0.0, 0.0)

    def _goal_callback(self, goal_request):
        """Accept a new goal, stopping any currently active run first."""
        del goal_request
        with self._active_goal_lock:
            old_goal = self._active_goal
        if old_goal is None:
            return GoalResponse.ACCEPT

        self.get_logger().info("New calibration goal received: stopping the current run first")
        self._request_stop_active_run("Superseded by a new calibration goal")

        # Wait for the old run's execute callback to actually exit and clear
        # itself, so its cleanup (_reset_calibration_session, restore_head, ...)
        # can't race with the new goal's. Bounded so a stuck run can't wedge
        # every future goal.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with self._active_goal_lock:
                if self._active_goal is None:
                    return GoalResponse.ACCEPT
            time.sleep(0.05)

        self.get_logger().warn("Rejecting calibration goal: previous run did not stop in time")
        return GoalResponse.REJECT

    def _cancel_callback(self, goal_handle):
        """Allow cancellation of the current run."""
        del goal_handle
        self._request_stop_active_run("Calibration cancel requested")
        return CancelResponse.ACCEPT

    def _publish_action_feedback(
        self,
        goal_handle,
        phase: str,
        message: str,
        corners_found: bool = False,
        images: dict[str, np.ndarray] | None = None,
    ):
        feedback = RunStereoCalibration.Feedback()
        feedback.phase = phase
        feedback.images_captured = int(self.images_captured)
        feedback.capture_attempts = int(self.capture_attempts)
        feedback.corners_found = bool(corners_found)
        feedback.message = message
        # Live value (not the cached attribute) so a mid-run `ros2 param set`
        # is reflected immediately, same as the watchdog itself.
        feedback.capture_timeout_sec = float(self.get_parameter("capture_timeout_sec").value)

        feedback.board = self.target_type
        view = self._last_view
        if view is not None:
            feedback.distance_hint = view.hint
            feedback.board_extent = float(view.extent)
            feedback.board_tilt = float(view.tilt)
        progress = self._coverage.progress()
        feedback.coverage_percent = float(progress.percent)
        feedback.coverage_missing = list(progress.missing)

        for name, img in (images or {}).items():
            ok, buf = cv2.imencode(".jpg", img)
            if not ok:
                continue
            compressed = CompressedImage()
            compressed.format = "jpeg"
            compressed.data = buf.tobytes()
            feedback.image_names.append(name)
            feedback.images.append(compressed)
        goal_handle.publish_feedback(feedback)

    def _request_stop_active_run(self, reason: str):
        """Request stop for the active run."""
        self._cancel_requested = True
        self.get_logger().info(f"{reason}; cancellation flag set")

    def _check_capture_timeout(self):
        """Watchdog: abort a stalled managed run if no capture arrives in time.

        Guards against an orphaned goal when the requesting (rosbridge) client
        disconnects mid-run — the client stops sending capture_trigger messages,
        so this timer notices and aborts the goal itself.
        """
        if not self._watchdog_active:
            return
        # Re-read live so `ros2 param set capture_timeout_sec` takes effect immediately,
        # instead of only at node startup.
        timeout_sec = float(self.get_parameter("capture_timeout_sec").value)
        if time.time() - self._last_capture_time > timeout_sec:
            self._watchdog_active = False
            self._watchdog_timed_out = True
            self.get_logger().warn(f"No capture received in {timeout_sec:.0f}s — aborting calibration run")

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
        calib_file = calib_dir / "stereo_calib.yaml"

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
        """Execute a managed stereo calibration run for app/API clients (manual capture only)."""
        with self._active_goal_lock:
            self._active_goal = goal_handle

        def _make_result(success: bool, message: str, timed_out: bool = False) -> RunStereoCalibration.Result:
            result = RunStereoCalibration.Result()
            result.success = success
            result.message = message
            result.timed_out = timed_out
            result.images_captured = int(self.images_captured)
            result.left_rms = float(self._last_rms["left"])
            result.right_rms = float(self._last_rms["right"])
            result.stereo_rms = float(self._last_rms["stereo"])
            result.quality = self._last_quality
            result.board = self.target_type
            return result

        try:
            goal = goal_handle.request
            self._cancel_requested = False
            self._reset_calibration_session()

            if goal.mode != RunStereoCalibration.Goal.MODE_MANUAL:
                msg = "Only MODE_MANUAL is currently supported"
                self.get_logger().error(msg)
                goal_handle.abort()
                return _make_result(False, msg)

            # Allow per-goal overrides while keeping sane defaults.
            if goal.num_images > 0:
                self.num_images_required = int(goal.num_images)
            if goal.min_corners > 0:
                self.min_corners = int(goal.min_corners)
            if goal.board in BOARD_BY_GOAL:
                self.target_type = BOARD_BY_GOAL[goal.board]
                self.get_logger().info(f"Goal selected the {self.target_type} target")
            # After the overrides: the tracker's capture target is one of them.
            self._coverage = guidance.CoverageTracker(self.num_images_required)

            setup_head(self)

            self._capture_enabled = True
            self._last_capture_time = time.time()
            self._watchdog_timed_out = False
            self._watchdog_active = True

            # One-shot "goal started" tick so the frontend can anchor a countdown
            # from goal-acceptance, not just after the first capture — otherwise a
            # slow first capture gets no warning before an unannounced timeout abort.
            self._publish_action_feedback(goal_handle, "READY", "Waiting for first capture")

            # Wait for capture_trigger events (handled in _enter_event_callback)
            # until enough images are captured, the goal is cancelled, or the
            # capture-timeout watchdog fires. Loop only ever exits via one of the
            # explicit branches below — a bare `while rclpy.ok():` would fall
            # through to "enough images captured" on shutdown too, which isn't true.
            while True:
                if not rclpy.ok():
                    self._capture_enabled = False
                    goal_handle.abort()
                    return _make_result(False, "Node shutting down")

                if self._cancel_requested or goal_handle.is_cancel_requested:
                    restore_head(self)
                    self._capture_enabled = False
                    # goal_handle.canceled() is only a valid transition after a real
                    # action-level cancel handshake (is_cancel_requested). The stop
                    # service sets _cancel_requested directly without that handshake,
                    # so it must abort instead or rclpy raises an invalid transition.
                    if goal_handle.is_cancel_requested:
                        goal_handle.canceled()
                    else:
                        goal_handle.abort()
                    return _make_result(False, "Calibration stopped")

                if self._watchdog_timed_out:
                    restore_head(self)
                    self._capture_enabled = False
                    msg = "Timed out waiting for capture"
                    goal_handle.abort()
                    return _make_result(False, msg, timed_out=True)

                if self.images_captured >= self.num_images_required:
                    break

                time.sleep(0.1)

            self._watchdog_active = False
            self._capture_enabled = False
            self._publish_action_feedback(
                goal_handle, "PROCESSING", f"Captured {self.images_captured} image pairs, running calibration"
            )

            success, message = self.run_calibration(
                save_decision=bool(goal.save_calibration),
                shutdown_on_complete=False,
            )

            result = _make_result(success, message)
            if success:
                goal_handle.succeed()
            else:
                goal_handle.abort()
            return result

        except Exception as e:
            self._capture_enabled = False
            self.get_logger().error(f"Managed calibration failed: {e}")
            goal_handle.abort()
            return _make_result(False, f"Calibration failed: {e}")
        finally:
            self._watchdog_active = False
            with self._active_goal_lock:
                # Identity check: _goal_callback waits for the previous run to
                # fully clear before accepting a new one, but guard anyway so a
                # slow-to-exit old run can never clobber a newer goal's slot.
                if self._active_goal is goal_handle:
                    self._active_goal = None

    def image_callback(self, left_msg, right_msg):
        """Store latest left and right frames."""
        if not self._capture_enabled:
            return
        try:
            # Convert left image
            if left_msg.encoding == "bgr8":
                left_frame = self.bridge.imgmsg_to_cv2(left_msg, "bgr8")
            elif left_msg.encoding == "rgb8":
                left_frame = self.bridge.imgmsg_to_cv2(left_msg, "rgb8")
                left_frame = cv2.cvtColor(left_frame, cv2.COLOR_RGB2BGR)
            else:
                left_frame = self.bridge.imgmsg_to_cv2(left_msg, "mono8")
                left_frame = cv2.cvtColor(left_frame, cv2.COLOR_GRAY2BGR)

            # Convert right image
            if right_msg.encoding == "bgr8":
                right_frame = self.bridge.imgmsg_to_cv2(right_msg, "bgr8")
            elif right_msg.encoding == "rgb8":
                right_frame = self.bridge.imgmsg_to_cv2(right_msg, "rgb8")
                right_frame = cv2.cvtColor(right_frame, cv2.COLOR_RGB2BGR)
            else:
                right_frame = self.bridge.imgmsg_to_cv2(right_msg, "mono8")
                right_frame = cv2.cvtColor(right_frame, cv2.COLOR_GRAY2BGR)

            # Debug: log frame info on first frame
            if self.latest_left_frame is None:
                self.get_logger().info(
                    f"Received first frames: left={left_frame.shape[1]}x{left_frame.shape[0]}, "
                    f"right={right_frame.shape[1]}x{right_frame.shape[0]}, "
                    f"expected={self.image_width}x{self.image_height}"
                )

            # Store frames
            with self.frame_lock:
                self.latest_left_frame = left_frame
                self.latest_right_frame = right_frame
                self.latest_stamps = (_stamp_sec(left_msg), _stamp_sec(right_msg))

        except Exception as e:
            self.get_logger().error(f"Failed to convert image: {e}")

    # ------------------------------------------------------------------ #
    #  Enter-event handling
    # ------------------------------------------------------------------ #

    def _enter_event_callback(self, msg: Bool):
        """Called on a capture_trigger event — grab latest frames and process.

        Used both by the CLI keyboard-input loop and by managed/action clients
        (published over rosbridge as a manual "capture now" request).
        """
        del msg
        if self.calibration_done or not self._capture_enabled:
            return

        with self.frame_lock:
            if self.latest_left_frame is None or self.latest_right_frame is None:
                self.get_logger().warn(
                    f"No frames available yet. Check the camera is running, and that the left/right "
                    f"stamps agree to within sync_slop_sec ({self.sync_slop_sec * 1000:.0f}ms)."
                )
                return
            left_img = self.latest_left_frame.copy()
            right_img = self.latest_right_frame.copy()
            stamps = self.latest_stamps

        self.capture_attempts += 1
        self._last_capture_time = time.time()
        result = self._process_image_pair(left_img, right_img, label="Capture", save_images=True, stamps=stamps)

        if result.success:
            self.get_logger().info(f"[{self.images_captured}] Captured! Detected {result.num_common} common corners.")

        # Generate debug mosaic after every capture attempt
        if self.debug:
            generate_debug_mosaic(self, result)

        with self._active_goal_lock:
            goal_handle = self._active_goal
        if goal_handle is not None:
            left_coverage, right_coverage = generate_coverage_images(self)
            feedback_message = (
                f"Captured {self.images_captured}/{self.num_images_required}"
                if result.success
                else self._rejection_advice()
            )
            self._publish_action_feedback(
                goal_handle,
                "CAPTURE",
                feedback_message,
                corners_found=result.success,
                images={"left_coverage": left_coverage, "right_coverage": right_coverage},
            )

    def check_existing_images(self):
        """Check for existing calibration images in /tmp and ask user if they want to use them."""
        # Find all left images
        left_images = sorted(self.tmp_image_dir.glob("left_*.png"))
        right_images = sorted(self.tmp_image_dir.glob("right_*.png"))

        if len(left_images) >= self.num_images_required and len(right_images) >= self.num_images_required:
            self.get_logger().info("")
            self.get_logger().info(f"Found {len(left_images)} existing calibration images in {self.tmp_image_dir}")
            print("")
            print(f"Found {len(left_images)} existing calibration images in {self.tmp_image_dir}")
            print("Do you want to use these images for calibration?")
            print('Type "y" to use existing images, "n" to capture new ones: ', end="", flush=True)

            try:
                response = input().strip().lower()
                if response == "y":
                    self.get_logger().info("Loading existing images...")
                    self.load_existing_images(
                        left_images[: self.num_images_required], right_images[: self.num_images_required]
                    )
                    return
                else:
                    # User chose to override - delete old images
                    self.get_logger().info("Deleting old calibration images...")
                    for img in left_images:
                        img.unlink()
                    for img in right_images:
                        img.unlink()
                    self.get_logger().info(f"Deleted {len(left_images) + len(right_images)} old images.")
            except EOFError:
                self.get_logger().info("No input, proceeding with new capture.")
            except Exception as e:
                self.get_logger().warn(f"Error reading input: {e}, proceeding with new capture.")

        # If we get here, proceed with normal capture
        self.get_logger().info("")
        self.get_logger().warn(">>> Press ENTER to capture an image <<<")
        self.get_logger().info("")

    def load_existing_images(self, left_image_paths, right_image_paths):
        """Load and process existing calibration images."""
        self.get_logger().info(f"Processing {len(left_image_paths)} existing images...")

        for idx, (left_path, right_path) in enumerate(zip(left_image_paths, right_image_paths), 1):  # noqa: B905
            try:
                left_img = cv2.imread(str(left_path))
                right_img = cv2.imread(str(right_path))

                if left_img is None or right_img is None:
                    self.get_logger().warn(f"Failed to load images: {left_path}, {right_path}")
                    continue

                result = self._process_image_pair(left_img, right_img, label=f"image {idx}")

                if result.success:
                    self.get_logger().info(
                        f"[{self.images_captured}/{self.num_images_required}] "
                        f"Loaded image {idx}: {result.num_common} common corners."
                    )
                    if self.images_captured >= self.num_images_required:
                        self.get_logger().info("")
                        self.get_logger().info("All images loaded! Computing calibration...")
                        self.calibration_done = True
                        self.run_calibration()
                        return

            except Exception as e:
                self.get_logger().error(f"Error processing image {idx}: {e}")
                continue

        # If we get here, we didn't get enough valid images
        if self.images_captured < self.num_images_required:
            self.get_logger().warn(
                f"Only loaded {self.images_captured} valid images out of {self.num_images_required} required."
            )
            self.get_logger().warn("Please capture new images.")
            self.images_captured = 0
            self.indiv_corners_left = []
            self.indiv_corners_right = []
            self.indiv_obj_points_left = []
            self.indiv_obj_points_right = []
            self.common_corners_left = []
            self.common_corners_right = []
            self.common_obj_points = []
            self.calibration_done = False
            self.get_logger().info("")
            self.get_logger().info(">>> Press ENTER to capture an image <<<")
            self.get_logger().info("")

    def keyboard_input_loop(self):
        """Background thread to handle keyboard input."""
        while rclpy.ok() and not self.calibration_done:
            try:
                # Wait for Enter key
                progress = self._coverage.progress()
                advice = self._coverage.advice()
                input(
                    f"[{self.images_captured} captured, {progress.percent:.0f}% covered] "
                    f"{advice or 'Coverage complete'} — move the board and press Enter "
                    f"(Ctrl+C to finish and calibrate)\n"
                )
                if not self.calibration_done:
                    self._enter_pub.publish(Bool(data=True))
            except (EOFError, KeyboardInterrupt):
                break

    def _process_image_pair(
        self,
        left_img,
        right_img,
        label="capture",
        save_images=False,
        stamps: tuple[float, float] = (0.0, 0.0),
    ) -> DetectionResult:
        """Detect the configured target in a stereo pair and store it if valid."""
        if self.target_type == TARGET_CHECKERBOARD:
            return self._process_checkerboard_pair(left_img, right_img, label, save_images, stamps)
        return self._process_charuco_pair(left_img, right_img, label, save_images, stamps)

    def _next_split(self) -> str:
        """Deterministic train/validation assignment for the next accepted pair.

        A fixed stride rather than a random draw so a rerun over the same
        captures reproduces the split exactly, and so held-out poses stay spread
        across the whole session instead of clustering at the end.
        """
        stride = self.validation_stride
        if stride <= 0:
            return "train"
        if self.target_type == TARGET_CHARUCO and not self.validate_charuco:
            return "train"
        return "validation" if (self.images_captured + 1) % stride == 0 else "train"

    def _process_checkerboard_pair(
        self,
        left_img,
        right_img,
        label: str,
        save_images: bool,
        stamps: tuple[float, float],
    ) -> DetectionResult:
        """Detect the full inner-corner grid in both images and store if complete."""
        left_gray = cv2.cvtColor(left_img, cv2.COLOR_BGR2GRAY)
        right_gray = cv2.cvtColor(right_img, cv2.COLOR_BGR2GRAY)

        pattern = self.checkerboard.pattern
        corners_left = cb.detect(left_gray, pattern)
        corners_right = cb.detect(right_gray, pattern)

        diagnostics_left = cb.measure(left_gray, corners_left, stamps[0])
        diagnostics_right = cb.measure(right_gray, corners_right, stamps[1])

        result = DetectionResult(left_img=left_img, right_img=right_img)
        accepted = corners_left is not None and corners_right is not None
        if accepted:
            reason = "ok"
        elif corners_left is None and corners_right is None:
            reason = "pattern incomplete in both images"
        else:
            reason = f"pattern incomplete in {'left' if corners_left is None else 'right'} image"

        split = self._next_split() if accepted else ""
        index = self.images_captured if accepted else -1
        self._note_view(corners_left, self.checkerboard.object_grid())
        if accepted:
            self._count_toward_coverage()

        self.capture_diagnostics.append(
            cb.CaptureDiagnostics(
                index=index,
                attempt=self.capture_attempts,
                accepted=accepted,
                reason=reason,
                split=split,
                stamp_skew_sec=stamps[0] - stamps[1],
                left=diagnostics_left,
                right=diagnostics_right,
                corners_left=corners_left,
                corners_right=corners_right,
            )
        )

        if not accepted:
            self.get_logger().warn(
                f"{label}: {reason} (need the complete {pattern[0]}x{pattern[1]} grid in BOTH images). "
                f"Brightness L/R {diagnostics_left.brightness:.1f}/{diagnostics_right.brightness:.1f}, "
                f"sharpness L/R {diagnostics_left.sharpness:.0f}/{diagnostics_right.sharpness:.0f}"
            )
            return result

        object_points = self.checkerboard.object_grid().reshape(-1, 1, 3)
        self.indiv_corners_left.append(corners_left)
        self.indiv_corners_right.append(corners_right)
        self.indiv_obj_points_left.append(object_points)
        self.indiv_obj_points_right.append(object_points)
        self.common_corners_left.append(corners_left)
        self.common_corners_right.append(corners_right)
        self.common_obj_points.append(object_points)

        if split == "validation":
            self.validation_indices.add(index)
        self.images_captured += 1

        if save_images and self.recorder is not None:
            self.recorder.save_pair(index, left_img, right_img)
            self.recorder.save_detection_overlay(index, left_img, right_img, corners_left, corners_right, pattern)

        result.success = True
        result.num_common = self.checkerboard.num_corners
        result.corners_left_filtered = corners_left
        result.corners_right_filtered = corners_right
        result.obj_pts_common = object_points
        self.get_logger().info(
            f"{label}: accepted [{index}] as {split} — skew {abs(stamps[0] - stamps[1]) * 1000.0:.1f}ms, "
            f"coverage L/R {diagnostics_left.coverage_pct:.0f}%/{diagnostics_right.coverage_pct:.0f}%"
        )
        return result

    def _process_charuco_pair(
        self,
        left_img,
        right_img,
        label: str = "capture",
        save_images: bool = False,
        stamps: tuple[float, float] = (0.0, 0.0),
    ) -> DetectionResult:
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
        self.get_logger().debug(f"Image brightness - Left mean: {left_mean:.1f}, Right mean: {right_mean:.1f}")

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
        charuco_corners_left, charuco_ids_left, marker_corners_left, marker_ids_left = (
            self.charuco_detector.detectBoard(left_gray)
        )
        charuco_corners_right, charuco_ids_right, marker_corners_right, marker_ids_right = (
            self.charuco_detector.detectBoard(right_gray)
        )

        left_markers = len(marker_ids_left) if marker_ids_left is not None else 0
        right_markers = len(marker_ids_right) if marker_ids_right is not None else 0
        left_corners = len(charuco_ids_left) if charuco_ids_left is not None else 0
        right_corners = len(charuco_ids_right) if charuco_ids_right is not None else 0

        view_left = cb.measure(left_gray, charuco_corners_left, stamps[0])
        view_right = cb.measure(right_gray, charuco_corners_right, stamps[1])

        # ChArUco detects a subset of the grid, so the board-space points come
        # from the corner ids rather than from a full pattern.
        if charuco_ids_left is not None and left_corners >= 4:
            self._note_view(charuco_corners_left, self.charuco_board.getChessboardCorners()[charuco_ids_left.flatten()])
        else:
            self._note_view(None, None)

        def record(accepted: bool, reason: str, split: str = "") -> None:
            self.capture_diagnostics.append(
                cb.CaptureDiagnostics(
                    index=self.images_captured if accepted else -1,
                    attempt=self.capture_attempts,
                    accepted=accepted,
                    reason=reason,
                    split=split,
                    stamp_skew_sec=stamps[0] - stamps[1],
                    left=view_left,
                    right=view_right,
                    corners_left=charuco_corners_left,
                    corners_right=charuco_corners_right,
                )
            )

        self.get_logger().info(
            f"Detection results - Left: {left_markers} markers, {left_corners} corners | "
            f"Right: {right_markers} markers, {right_corners} corners"
        )

        # Explain why corners might not be interpolated
        if left_markers > 0 and left_corners == 0:
            self.get_logger().warn(
                f"Left: {left_markers} markers detected but 0 corners interpolated. "
                f"Possible causes: markers not in complete grid, board partially visible, "
                f"or board parameters (squares_x={self.squares_x}, squares_y={self.squares_y}) don't match."
            )
        if right_markers > 0 and right_corners == 0:
            self.get_logger().warn(
                f"Right: {right_markers} markers detected but 0 corners interpolated. "
                f"Possible causes: markers not in complete grid, board partially visible, "
                f"or board parameters (squares_x={self.squares_x}, squares_y={self.squares_y}) don't match."
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
                f"{label}: Not enough corners detected! Left: {left_corners}, Right: {right_corners} "
                f"(need {self.min_corners}+)."
            )
            self.get_logger().warn(
                f"  Image brightness - Left: {left_mean:.1f}, Right: {right_mean:.1f} (typical range: 50-200)"
            )
            self.get_logger().warn(f"  Markers detected - Left: {left_markers}, Right: {right_markers}")
            self.get_logger().warn(
                f"  Make sure ChArUco board ({self.squares_x}x{self.squares_y}) is fully visible "
                f"in BOTH camera views with good lighting."
            )
            # Save diagnostic images
            try:
                debug_dir = self.data_directory / "calibration_debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                import time

                timestamp = int(time.time())
                cv2.imwrite(str(debug_dir / f"left_{timestamp}.png"), left_img)
                cv2.imwrite(str(debug_dir / f"right_{timestamp}.png"), right_img)
                self.get_logger().info(f"  Saved diagnostic images to: {debug_dir}")
            except Exception as e:
                self.get_logger().debug(f"Could not save diagnostic images: {e}")
            record(False, f"too few corners (L{left_corners}/R{right_corners}, need {self.min_corners})")
            return result

        # Find common corner IDs between left and right
        if charuco_ids_left is None or charuco_ids_right is None:
            self.get_logger().warn(f"{label}: ChArUco board not detected in one or both images.")
            record(False, "board not detected in one or both images")
            return result

        left_ids_set = set(charuco_ids_left.flatten())
        right_ids_set = set(charuco_ids_right.flatten())
        common_ids = left_ids_set & right_ids_set

        if len(common_ids) < self.min_corners:
            self.get_logger().warn(
                f"{label}: Not enough common corners! Common: {len(common_ids)} (need {self.min_corners}+)."
            )
            record(False, f"too few common corners ({len(common_ids)}, need {self.min_corners})")
            return result

        # Filter to keep only common corners, sorted by ID
        left_mask = [i for i, id_val in enumerate(charuco_ids_left.flatten()) if id_val in common_ids]
        right_mask = [i for i, id_val in enumerate(charuco_ids_right.flatten()) if id_val in common_ids]

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

        index = self.images_captured
        split = self._next_split()
        if split == "validation":
            self.validation_indices.add(index)
        record(True, "ok", split)
        self._count_toward_coverage()
        self.images_captured += 1

        # Optionally save images to disk. Indexed before the increment so the
        # filename matches the capture index that validation_indices stores.
        if save_images:
            try:
                cv2.imwrite(str(self.tmp_image_dir / f"left_{index:03d}.png"), left_img)
                cv2.imwrite(str(self.tmp_image_dir / f"right_{index:03d}.png"), right_img)
            except Exception as e:
                self.get_logger().warn(f"Failed to save images: {e}")

        result.success = True
        result.num_common = len(common_ids)
        result.common_ids = common_ids
        result.corners_left_filtered = corners_left_filtered
        result.corners_right_filtered = corners_right_filtered
        result.obj_pts_common = obj_pts_common
        return result

    def run_calibration(
        self,
        save_decision: bool | None = None,
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
        train = [i for i in range(self.images_captured) if i not in self.validation_indices]

        def keep(values: list) -> list:
            return [values[i] for i in train]

        self.get_logger().info("Running individual camera calibrations...")
        self.get_logger().info(
            f"  Individual points: {len(self.indiv_obj_points_left)} images, "
            f"Common points: {len(self.common_obj_points)} images"
        )
        if self.validation_indices:
            self.get_logger().info(
                f"  Held out {len(self.validation_indices)} pairs for validation; calibrating on {len(train)}"
            )

        # # Debug: check shapes
        # for i, (obj_l, corners_l) in enumerate(zip(self.indiv_obj_points_left, self.indiv_corners_left)):
        #     self.get_logger().debug(f'  Left  img {i+1}: obj={obj_l.shape}, corners={corners_l.shape}')
        # for i, (obj_r, corners_r) in enumerate(zip(self.indiv_obj_points_right, self.indiv_corners_right)):
        #     self.get_logger().debug(f'  Right img {i+1}: obj={obj_r.shape}, corners={corners_r.shape}')

        # Calibrate left camera using ALL left-camera corners (not just common)
        ret_left, K1, D1, rvecs_left, tvecs_left = cv2.calibrateCamera(
            keep(self.indiv_obj_points_left), keep(self.indiv_corners_left), image_size, None, None, flags=0
        )
        self.get_logger().info(f"Left camera RMS error: {ret_left:.4f}")

        # Calibrate right camera using ALL right-camera corners (not just common)
        ret_right, K2, D2, rvecs_right, tvecs_right = cv2.calibrateCamera(
            keep(self.indiv_obj_points_right), keep(self.indiv_corners_right), image_size, None, None, flags=0
        )
        self.get_logger().info(f"Right camera RMS error: {ret_right:.4f}")

        self.get_logger().info("Running stereo calibration...")

        # Stereo calibration uses COMMON corners only (matched across both cameras)
        flags = cv2.CALIB_FIX_INTRINSIC

        ret_stereo, K1, D1, K2, D2, R, T, E, F = cv2.stereoCalibrate(
            keep(self.common_obj_points),
            keep(self.common_corners_left),
            keep(self.common_corners_right),
            K1,
            D1,
            K2,
            D2,
            image_size,
            flags=flags,
            criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6),
        )

        # UNDER REVIEW — see docs/stereo_calibration_experiment.md. stereoCalibrate
        # returns the left camera's origin in the right camera's frame, so a
        # correctly-wired rig gives T[0] < 0 and this branch always fires. It is
        # kept for now because every downstream consumer takes |baseline|; the
        # measured consequence is a sign-flipped Q, recorded in the run summary.
        t_x_raw = float(T[0, 0])
        if T[0, 0] < 0:
            self.get_logger().warn(f"T[0] = {T[0, 0]:.4f}m is negative - cameras may be physically swapped")
            self.get_logger().warn("Negating T to ensure positive depth output")
            T = -T

        self.get_logger().info(f"Stereo RMS error: {ret_stereo:.4f}")

        # Compute rectification transforms
        R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(
            K1,
            D1,
            K2,
            D2,
            image_size,
            R,
            T,
            alpha=0,  # 0 = crop to valid pixels only
            flags=cv2.CALIB_ZERO_DISPARITY,
        )

        # Calculate baseline
        baseline = np.linalg.norm(T)
        focal_length = Q[2, 3]

        # Print results
        self.get_logger().info("")
        self.get_logger().info("=" * 60)
        self.get_logger().info("CALIBRATION RESULTS")
        self.get_logger().info("=" * 60)
        self.get_logger().info(f"Left camera RMS:   {ret_left:.4f} pixels")
        self.get_logger().info(f"Right camera RMS:  {ret_right:.4f} pixels")
        self.get_logger().info(f"Stereo RMS:        {ret_stereo:.4f} pixels")
        self.get_logger().info(f"Baseline:          {baseline * 1000:.2f} mm")
        self.get_logger().info(f"Focal length:      {focal_length:.2f} pixels")
        self.get_logger().info("=" * 60)
        self.get_logger().info("")

        # Quality assessment
        if ret_stereo < 0.5:
            quality = "EXCELLENT"
        elif ret_stereo < 1.0:
            quality = "GOOD"
        elif ret_stereo < 2.0:
            quality = "ACCEPTABLE"
        else:
            quality = "POOR - consider recalibrating"

        self.get_logger().info(f"Calibration quality: {quality}")
        self.get_logger().info("")
        self._last_rms = {
            "left": float(ret_left),
            "right": float(ret_right),
            "stereo": float(ret_stereo),
        }
        self._last_quality = quality

        # Store calibration data for saving (must be before generate_visualizations)
        self.calibration_data = {
            "K1": K1,
            "D1": D1,
            "K2": K2,
            "D2": D2,
            "R": R,
            "T": T,
            "R1": R1,
            "R2": R2,
            "P1": P1,
            "P2": P2,
            "Q": Q,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "rms_error": ret_stereo,
            "version": 2,
        }

        # Generate visualization images
        self.get_logger().info("Generating visualization images...")
        generate_visualizations(self)

        if self.recorder is not None:
            experiment_message = self._write_experiment_outputs(
                self.recorder, image_size, t_x_raw, ret_left, ret_right, ret_stereo
            )
            restore_head(self)
            if shutdown_on_complete and rclpy.ok():
                rclpy.shutdown()
            return True, experiment_message

        # Interactive CLI mode: ask user and terminate through prompt_save().
        if save_decision is None:
            prompt_save(self)
            return True, "Calibration complete"

        # Managed mode: save/discard without stdin interaction.
        try:
            if save_decision:
                output_path = save_calibration(self)
                msg = f"Calibration saved to {output_path or ''}"
            else:
                msg = "Calibration computed and discarded"
                self.get_logger().info(msg)
            restore_head(self)
            if shutdown_on_complete and rclpy.ok():
                rclpy.shutdown()
            return True, msg
        except Exception as e:
            restore_head(self)
            return False, f"Failed to finalize calibration: {e}"

    def _validation_pairs(self) -> list[ValidationPair]:
        """Held-out observations, in capture order."""
        grid_shape = self.checkerboard.pattern if self.target_type == TARGET_CHECKERBOARD else None
        return [
            ValidationPair(
                index=i,
                object_points=self.common_obj_points[i],
                corners_left=self.common_corners_left[i],
                corners_right=self.common_corners_right[i],
                grid_shape=grid_shape,
            )
            for i in sorted(self.validation_indices)
        ]

    def _write_experiment_outputs(
        self,
        recorder: ExperimentRecorder,
        image_size: tuple[int, int],
        t_x_raw: float,
        ret_left: float,
        ret_right: float,
        ret_stereo: float,
    ) -> str:
        """Write the candidate calibration, capture metadata and validation report.

        Never writes the production stereo_calib.yaml — the whole point of the
        experiment arm is that the shipped calibration stays untouched.
        """
        calib = self.calibration_data
        candidate_path = recorder.save_candidate_calibration(calib)
        recorder.write_captures(self.capture_diagnostics)
        recorder.save_coverage(self.indiv_corners_left, self.indiv_corners_right, image_size)

        square_size = (
            self.checkerboard.square_size if self.target_type == TARGET_CHECKERBOARD else float(self.square_size)
        )
        pairs = self._validation_pairs()
        report = None
        if pairs:
            report = evaluate(
                StereoCalibrationMatrices(
                    K1=calib["K1"],
                    D1=calib["D1"],
                    K2=calib["K2"],
                    D2=calib["D2"],
                    R1=calib["R1"],
                    R2=calib["R2"],
                    P1=calib["P1"],
                    P2=calib["P2"],
                ),
                pairs,
                image_size,
                square_size,
            )
            recorder.write_validation(report)
            self._log_validation(report)
        else:
            self.get_logger().warn("No validation pairs were held out — skipping the held-out report")

        self._save_rectified_samples(recorder, calib, image_size, pairs)

        skew = skew_summary(self.capture_diagnostics)
        recorder.write_summary(
            {
                "target_type": self.target_type,
                "pattern": (
                    list(self.checkerboard.pattern)
                    if self.target_type == TARGET_CHECKERBOARD
                    else [self.squares_x, self.squares_y]
                ),
                "square_size_m": square_size,
                "image_size": list(image_size),
                "pairs_accepted": self.images_captured,
                "capture_attempts": self.capture_attempts,
                "pairs_train": self.images_captured - len(self.validation_indices),
                "pairs_validation": len(self.validation_indices),
                "validation_indices": sorted(self.validation_indices),
                "validation_stride": self.validation_stride,
                "train_rms": {"left": ret_left, "right": ret_right, "stereo": ret_stereo},
                "baseline_m": float(np.linalg.norm(calib["T"])),
                "stereo_calibrate_tx_raw_m": t_x_raw,
                "tx_was_negated": t_x_raw < 0.0,
                "sync_slop_sec": self.sync_slop_sec,
                "timestamp_skew": skew,
                "validation": report.to_dict() if report is not None else None,
            }
        )

        self.get_logger().info("=" * 60)
        self.get_logger().info(f"Experiment written to: {recorder.run_dir}")
        self.get_logger().info(
            f"  Candidate calibration: {candidate_path.name} (production stereo_calib.yaml UNTOUCHED)"
        )
        if skew.get("count"):
            self.get_logger().info(
                f"  Timestamp skew: mean {skew['mean_ms']:.2f}ms, median {skew['median_ms']:.2f}ms, "
                f"p95 {skew['p95_ms']:.2f}ms, max {skew['max_ms']:.2f}ms "
                f"(sync slop {self.sync_slop_sec * 1000:.0f}ms)"
            )
        self.get_logger().info("=" * 60)
        return f"{self.target_type} experiment complete: {recorder.run_dir}"

    def _save_rectified_samples(self, recorder, calib, image_size, pairs) -> None:
        """Re-read a few saved captures and render them rectified with guide lines."""
        sample_indices = [p.index for p in pairs[:4]] or list(range(min(4, self.images_captured)))
        samples = []
        for index in sample_indices:
            left = cv2.imread(str(recorder.image_dir / f"left_{index:03d}.png"))
            right = cv2.imread(str(recorder.image_dir / f"right_{index:03d}.png"))
            if left is not None and right is not None:
                samples.append((index, left, right))
        if samples:
            recorder.save_rectified_samples(samples, calib, image_size)

    def _log_validation(self, report) -> None:
        self.get_logger().info(f"Held-out validation on {report.num_pairs} pairs:")
        self.get_logger().info(
            f"  Reprojection RMS  L {report.left_reprojection.rms:.4f}px  R {report.right_reprojection.rms:.4f}px"
        )
        self.get_logger().info(
            f"  Epipolar |dy|     mean {report.epipolar_dy.mean:.4f}px  p95 {report.epipolar_dy.p95:.4f}px  "
            f"max {report.epipolar_dy.maximum:.4f}px"
        )
        if report.spacing_error_mm.count:
            self.get_logger().info(
                f"  Neighbour spacing mean {report.mean_spacing_mm:.3f}mm vs nominal "
                f"{report.nominal_spacing_mm:.3f}mm (mean abs error {report.spacing_error_mm.mean:.3f}mm)"
            )
        else:
            self.get_logger().info("  Neighbour spacing n/a (needs a complete grid; ChArUco returns corner subsets)")
        self.get_logger().info(f"  Planarity RMS     {report.planarity_rms_mm.mean:.3f}mm")
        if not report.q_yields_positive_z:
            self.get_logger().warn(
                f"  Q reprojects front-of-camera points to NEGATIVE Z "
                f"(median {report.median_depth_m:+.3f}m) — see the T-sign finding in the report"
            )


def main(args=None):
    rclpy.init(args=args)

    node = StereoCalibrator()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)

    try:
        executor.spin()
    except KeyboardInterrupt:
        if not node.calibration_done and node.images_captured > 0:
            print(f"\n\nCtrl+C received — running calibration with {node.images_captured} captured images...\n")
            node.run_calibration()
        elif node.images_captured == 0:
            print("\nCtrl+C received — no images captured, exiting.")
    except Exception as e:
        # "context is not valid" is expected on SIGTERM / normal shutdown — ignore it
        if "context is not valid" not in str(e):
            print(f"[stereo_calibrator] Error in main: {e}", file=sys.stderr)

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


if __name__ == "__main__":
    main()
