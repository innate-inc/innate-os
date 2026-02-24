"""
Gaze System - Person tracking for MARS robot.

Features:
- InspireFace detection for autonomous person tracking
- Wheel-based panning (robot turns to face people)

Hardware: MARS robot
- Head tilt: -25° to +15° (single axis)
- Pan: Uses differential drive wheels to rotate body
"""

import math
import time
import threading
from typing import Optional, Callable, List, Tuple

import cv2
import numpy as np

from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import Image

from brain_client.head_interface import HeadInterface
from brain_client.mobility_interface import MobilityInterface

import inspireface as isf


class FaceDetector:
    """Face detector using InspireFace SDK."""

    def __init__(self, min_confidence: float = 0.5):
        param = isf.SessionCustomParameter()
        self._session = isf.InspireFaceSession(
            param=param,
            detect_mode=isf.HF_DETECT_MODE_ALWAYS_DETECT,
            max_detect_num=3,
        )
        self._session.set_detection_confidence_threshold(min_confidence)

    def detect(self, frame) -> List[dict]:
        """Detect faces, return list of {center_x, center_y, width, height}."""
        h, w = frame.shape[:2]
        faces = []
        for face in self._session.face_detection(frame):
            x1, y1, x2, y2 = face.location
            faces.append({
                "center_x": (x1 + x2) / 2 / w,
                "center_y": (y1 + y2) / 2 / h,
                "width": (x2 - x1) / w,
                "height": (y2 - y1) / h,
            })
        return faces


class GazeController:
    """Controls head tilt and wheel pan to track faces."""

    # Hardware limits
    MIN_TILT = -25  # degrees (looking down)
    MAX_TILT = 15   # degrees (looking up)

    # Camera parameters
    CAMERA_HFOV = 100.0  # horizontal FOV degrees
    CAMERA_VFOV = 50.0   # vertical FOV degrees

    # Pan parameters (from original)
    PAN_GAIN = 0.4       # rad/s per unit offset
    PAN_COOLDOWN = 0.5   # seconds between pan adjustments
    PAN_THRESHOLD = 5.0  # degrees - only pan if error exceeds this

    def __init__(
        self,
        head_command_fn: Callable[[int], None],
        wheel_rotate_fn: Optional[Callable[[float, float], None]] = None,
    ):
        self._head_command = head_command_fn
        self._wheel_rotate = wheel_rotate_fn

        self._current_tilt = 0.0
        self._target_tilt = 0.0
        self._last_commanded_tilt = 0

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._last_pan_time = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def track_face(self, face: dict, frame_shape: Tuple[int, int]):
        """Track a detected face by pointing at its center."""
        # Pan error (positive = face is on right = turn right)
        pan_error = (face["center_x"] - 0.5) * self.CAMERA_HFOV

        # Tilt: proportional control
        error_normalized = 0.5 - face["center_y"]
        tilt_error_degrees = error_normalized * self.CAMERA_VFOV
        Kp = 0.3
        tilt_correction = tilt_error_degrees * Kp

        with self._lock:
            new_tilt = self._current_tilt + tilt_correction
            self._target_tilt = max(self.MIN_TILT, min(self.MAX_TILT, new_tilt))

        # Execute pan if significant
        if abs(pan_error) > self.PAN_THRESHOLD:
            self._execute_pan(pan_error)

    def _execute_pan(self, pan_degrees: float):
        """Execute pan via wheel rotation (rate limited)."""
        if not self._wheel_rotate:
            return

        now = time.time()
        if now - self._last_pan_time < self.PAN_COOLDOWN:
            return

        # Positive pan = face is right = rotate right (negative angular velocity)
        angular_speed = -math.copysign(self.PAN_GAIN, pan_degrees)
        duration = min(abs(pan_degrees) / 30.0, 0.5)  # Cap duration

        if duration > 0.05:
            self._wheel_rotate(angular_speed, duration)
            self._last_pan_time = now

    def _loop(self):
        """Main tilt control loop at ~30Hz."""
        dt = 1.0 / 30.0

        while self._running:
            loop_start = time.time()

            with self._lock:
                tilt_int = int(round(self._target_tilt))
                tilt_int = max(self.MIN_TILT, min(self.MAX_TILT, tilt_int))

            if tilt_int != self._last_commanded_tilt:
                self._head_command(tilt_int)
                self._last_commanded_tilt = tilt_int

            with self._lock:
                self._current_tilt = self._target_tilt

            elapsed = time.time() - loop_start
            if elapsed < dt:
                time.sleep(dt - elapsed)


class ROSPersonTracker:
    """ROS2 person tracker - simple interface for agents."""

    def __init__(self, node, camera_topic: str = "/mars/main_camera/left/image_raw"):
        self._node = node
        self._frame = None
        self._frame_lock = threading.Lock()

        # Hardware interfaces
        self._head = HeadInterface(node, node.get_logger())
        self._mobility = MobilityInterface(node, node.get_logger(), "/cmd_vel")

        # Gaze controller
        self._gaze = GazeController(
            head_command_fn=self._head.set_position,
            wheel_rotate_fn=self._mobility.rotate_in_place,
        )
        self._detector: Optional[FaceDetector] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Face tracking state
        self._last_face_time = 0.0
        self._face_timeout = 5.0

        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        self._sub = node.create_subscription(Image, camera_topic, self._on_image, qos)

    def _on_image(self, msg):
        """Store latest camera frame."""
        try:
            frame = np.frombuffer(msg.data, dtype=np.uint8).reshape(
                msg.height, msg.width, -1
            )
            if msg.encoding == "rgb8":
                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            with self._frame_lock:
                self._frame = frame
        except Exception:
            pass

    def start(self):
        """Start person tracking."""
        if self._running:
            return
        self._running = True
        self._gaze.start()
        self._thread = threading.Thread(target=self._track_loop, daemon=True)
        self._thread.start()
        # Lazy init detector in background
        if self._detector is None:
            threading.Thread(target=self._init_detector, daemon=True).start()

    def _init_detector(self):
        try:
            self._detector = FaceDetector(min_confidence=0.3)
            self._node.get_logger().info("👁️ Face detector initialized")
        except Exception as e:
            self._node.get_logger().error(f"Failed to init face detector: {e}")

    def stop(self):
        """Stop person tracking."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._gaze.stop()

    @property
    def is_running(self) -> bool:
        return self._running

    def _track_loop(self):
        """Perception loop at ~5Hz."""
        dt = 1.0 / 5.0

        while self._running:
            loop_start = time.time()

            if self._detector is None:
                time.sleep(0.1)
                continue

            with self._frame_lock:
                frame = self._frame

            if frame is not None:
                shape = (frame.shape[0], frame.shape[1])
                faces = self._detector.detect(frame)

                if faces:
                    # Track largest face
                    best = max(faces, key=lambda f: f["width"] * f["height"])
                    self._gaze.track_face(best, shape)
                    self._last_face_time = time.time()
                elif time.time() - self._last_face_time > self._face_timeout:
                    # Return to neutral after timeout
                    with self._gaze._lock:
                        self._gaze._target_tilt = 0.0
                    self._last_face_time = time.time()

            elapsed = time.time() - loop_start
            if elapsed < dt:
                time.sleep(dt - elapsed)
