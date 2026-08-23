# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pose-driven person tracking for the MARS head and mobile base."""

import math
import threading
import time
from collections.abc import Callable

import cv2
import numpy as np
import numpy.typing as npt
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image

from brain_client.perception.face_lock import FaceBox, FaceLock, FaceLockResult
from brain_client.perception.gaze_debug import DebugFollow, GazeDebug, GazeStatus, gaze_debug
from brain_client.perception.gaze_nav import GazeNavigator
from brain_client.perception.person_follow import FollowStartResult, FollowTarget, PersonFollowController
from brain_client.perception.pose import Pose
from brain_client.perception.yolo_pose import PersonPose, YoloPoseDetector
from brain_client.robot.head import Head
from brain_client.robot.mobility import Mobility

_FOLLOW_STALE_SECONDS = 0.75


class FaceFollower:
    """Controls head tilt and wheel pan to track faces."""

    # Hardware limits
    MIN_TILT = -25  # degrees (looking down)
    MAX_TILT = 35  # degrees (looking up)

    # Camera parameters
    CAMERA_HFOV = 100.0  # horizontal FOV degrees
    CAMERA_VFOV = 50.0  # vertical FOV degrees

    # Pan parameters (from original)
    PAN_GAIN = 0.4  # rad/s per unit offset
    PAN_COOLDOWN = 0.5  # seconds between pan adjustments
    PAN_THRESHOLD = 5.0  # degrees - only pan if error exceeds this

    def __init__(
        self,
        head_command_fn: Callable[[int], None],
        wheel_rotate_fn: Callable[[float, float], None] | None = None,
    ) -> None:
        self._head_command = head_command_fn
        self._wheel_rotate = wheel_rotate_fn

        self._current_tilt = 0.0
        self._target_tilt = 0.0
        self._last_commanded_tilt = 0

        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._last_pan_time = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def look_at(self, face: FaceBox) -> None:
        """Point the head at a detected face."""
        error_normalized = 0.5 - face.center_y
        tilt_error_degrees = error_normalized * self.CAMERA_VFOV
        Kp = 0.3
        tilt_correction = tilt_error_degrees * Kp

        with self._lock:
            new_tilt = self._current_tilt + tilt_correction
            self._target_tilt = max(self.MIN_TILT, min(self.MAX_TILT, new_tilt))

    def pan_toward(self, face: FaceBox) -> None:
        """Turn the base toward a face when Nav2 does not own the base."""
        pan_error = (face.center_x - 0.5) * self.CAMERA_HFOV
        if abs(pan_error) > self.PAN_THRESHOLD:
            self._execute_pan(pan_error)

    def _execute_pan(self, pan_degrees: float) -> None:
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

    def _loop(self) -> None:
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


class ROSFaceTracker:
    """Detect, lock, and follow faces from a ROS camera stream."""

    def __init__(
        self,
        node: Node,
        camera_topic: str = "/mars/main_camera/left/image_raw",
        cmd_vel_topic: str = "/cmd_vel",
        *,
        get_odom_pose: Callable[[], Pose | None],
        get_navigation_mode: Callable[[], str | None],
        on_person_locked: Callable[[], None] | None = None,
        on_debug: Callable[[GazeDebug], None] | None = None,
    ) -> None:
        self._node = node
        self._on_person_locked = on_person_locked
        self._on_debug = on_debug
        self._last_debug: GazeDebug | None = None
        self._frame: tuple[int, npt.NDArray[np.uint8]] | None = None
        self._frame_number = 0
        self._processed_frame_number = 0
        self._frame_lock = threading.Lock()
        self._closed = False

        # Hardware interfaces
        self._head = Head(node, node.get_logger())
        self._mobility = Mobility(node, node.get_logger(), cmd_vel_topic)

        # Gaze controller
        self._follower = FaceFollower(
            head_command_fn=self._head.set_position,
            wheel_rotate_fn=self._mobility.rotate_in_place,
        )
        self._detector: YoloPoseDetector | None = None
        self._detector_error = ""
        self._detector_starting = False
        self._detector_thread: threading.Thread | None = None
        self._detector_lock = threading.Lock()
        self._face_lock = FaceLock()
        self._person_follow = PersonFollowController()
        self._gaze_navigator = GazeNavigator(
            node,
            node.get_logger(),
            get_odom_pose=get_odom_pose,
            get_navigation_mode=get_navigation_mode,
        )
        self._follow_lock = threading.Lock()
        self._locked_person: PersonPose | None = None
        self._follow_target: FollowTarget | None = None
        self._follow_stop_reason = ""
        self._last_detection_at = 0.0

        self._running = False
        self._thread: threading.Thread | None = None

        # Face tracking state
        self._last_face_time = 0.0
        self._face_timeout = 5.0

        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        self._sub = node.create_subscription(Image, camera_topic, self._on_image, qos)
        self._follow_watchdog = node.create_timer(0.1, self._check_follow_watchdog)

    def _on_image(self, msg: Image) -> None:
        """Store latest camera frame."""
        try:
            frame = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, -1)
            if msg.encoding == "rgb8":
                frame = np.asarray(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), dtype=np.uint8)
            else:
                frame = frame.copy()
            with self._frame_lock:
                self._frame_number += 1
                self._frame = (self._frame_number, frame)
        except Exception:
            pass

    def start(self) -> None:
        if self._running:
            return
        if self._closed:
            raise RuntimeError("cannot restart a closed gaze tracker")
        self._running = True
        with self._follow_lock:
            self._follow_stop_reason = ""
        self._face_lock.resume(time.monotonic())
        detector = self._detector_snapshot()
        self._emit_debug(
            gaze_debug(
                GazeStatus.ERROR
                if detector is None and self._detector_error
                else (GazeStatus.STARTING if detector is None else GazeStatus.SEARCHING),
                error=self._detector_error,
            )
        )
        self._follower.start()
        self._thread = threading.Thread(target=self._track_loop, daemon=True)
        self._thread.start()
        self._ensure_detector()

    def _ensure_detector(self) -> None:
        with self._detector_lock:
            if self._detector is not None or self._detector_starting or self._detector_error:
                return
            self._detector_starting = True
            self._detector_thread = threading.Thread(target=self._init_detector, daemon=True)
            detector_thread = self._detector_thread
        detector_thread.start()

    def _init_detector(self) -> None:
        detector: YoloPoseDetector | None = None
        error = ""
        try:
            detector = YoloPoseDetector()
            self._node.get_logger().info("👁️ YOLO pose detector initialized")
        except Exception as e:
            error = str(e)
            self._node.get_logger().error(f"Failed to init YOLO pose detector: {e}")
        finally:
            with self._detector_lock:
                self._detector = None if self._closed else detector
                self._detector_error = error
                self._detector_starting = False
        if self._running:
            self._emit_debug(
                gaze_debug(
                    GazeStatus.SEARCHING if detector is not None else GazeStatus.ERROR,
                    error=error,
                )
            )

    def pause(self) -> None:
        self.stop_follow("gaze paused")
        self._face_lock.pause(time.monotonic())
        self._running = False
        if self._thread:
            self._thread.join()
            self._thread = None
        self._follower.stop()
        self._mobility.stop()

    def close(self) -> None:
        if self._closed:
            return
        self.pause()
        self._closed = True
        detector_thread = self._detector_thread
        if detector_thread is not None and detector_thread.is_alive():
            detector_thread.join()
        self._node.destroy_subscription(self._sub)
        self._node.destroy_timer(self._follow_watchdog)
        self._gaze_navigator.close()
        self._head.close()
        self._mobility.close()
        with self._frame_lock:
            self._frame = None
        with self._detector_lock:
            self._detector = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_following(self) -> bool:
        with self._follow_lock:
            return self._person_follow.is_following

    def start_follow(self) -> FollowStartResult:
        if not self._running:
            return FollowStartResult.NOT_RUNNING
        with self._follow_lock:
            if self._person_follow.is_following:
                return FollowStartResult.ALREADY_FOLLOWING
            if self._locked_person is None or time.monotonic() - self._last_detection_at > _FOLLOW_STALE_SECONDS:
                return FollowStartResult.NO_LOCK
            self._person_follow.start(self._locked_person.body)
            self._follow_target = None
            self._follow_stop_reason = ""
        self._mobility.stop()
        self._emit_follow_state()
        return FollowStartResult.STARTED

    def stop_follow(self, reason: str = "") -> None:
        with self._follow_lock:
            self._person_follow.stop()
            self._follow_target = None
            self._follow_stop_reason = reason
        self._cancel_follow_motion(reason)
        self._emit_follow_state()

    def _stop_for_target_loss(self) -> None:
        with self._follow_lock:
            self._follow_stop_reason = "target lost"
        self._cancel_follow_motion("target lost")
        self._emit_follow_state()

    def _cancel_follow_motion(self, reason: str) -> None:
        self._gaze_navigator.cancel(reason)
        self._mobility.stop()

    def _track_loop(self) -> None:
        dt = 1.0 / 5.0

        while self._running:
            loop_start = time.time()

            detector = self._detector_snapshot()
            if detector is None:
                status = GazeStatus.ERROR if self._detector_error else GazeStatus.STARTING
                self._emit_debug(gaze_debug(status, error=self._detector_error))
                time.sleep(0.5)
                continue

            with self._frame_lock:
                sample = self._frame

            if sample is not None and sample[0] != self._processed_frame_number:
                self._processed_frame_number, frame = sample
                now = time.monotonic()
                try:
                    detection = detector.detect(frame)
                except Exception as e:  # noqa: BLE001 — one bad frame must not kill long-lived gaze
                    error = str(e)
                    if error != self._detector_error:
                        self._node.get_logger().error(f"YOLO pose inference failed: {e}")
                        self._detector_error = error
                    self.stop_follow("pose inference failed")
                    self._emit_debug(
                        gaze_debug(
                            GazeStatus.ERROR,
                            error=error,
                            image_width=frame.shape[1],
                            image_height=frame.shape[0],
                            frame=self._processed_frame_number,
                            follow=self._follow_debug(),
                        )
                    )
                    continue
                if not self._running:
                    break
                self._last_detection_at = now
                self._detector_error = ""
                faces = [person.head for person in detection.people]
                result = self._face_lock.observe(faces, now)
                locked_person = self._locked_person_for(detection.people, result)
                with self._follow_lock:
                    self._locked_person = locked_person
                    follow_was_active = self._person_follow.is_following
                    follow_target = self._person_follow.observe(
                        locked_person.body if locked_person is not None else None
                    )
                    self._follow_target = follow_target

                if follow_was_active and follow_target is None:
                    self._stop_for_target_loss()
                elif follow_target is not None:
                    self._gaze_navigator.update_target(follow_target)
                    if self._gaze_navigator.failed:
                        self.stop_follow(self._gaze_navigator.reason)

                self._emit_debug(
                    gaze_debug(
                        self._status(result),
                        faces=faces,
                        target=result.face,
                        people=detection.people,
                        progress=result.progress,
                        inference_ms=detection.inference_ms,
                        image_width=frame.shape[1],
                        image_height=frame.shape[0],
                        frame=self._processed_frame_number,
                        follow=self._follow_debug(),
                    )
                )
                if result.face is not None:
                    self._follower.look_at(result.face)
                    if not self.is_following:
                        self._follower.pan_toward(result.face)
                    self._last_face_time = time.time()
                    if result.just_locked and self._on_person_locked is not None:
                        self._on_person_locked()

            if time.time() - self._last_face_time > self._face_timeout:
                # Return to neutral after timeout
                with self._follower._lock:
                    self._follower._target_tilt = 0.0
                self._last_face_time = time.time()

            elapsed = time.time() - loop_start
            if elapsed < dt:
                time.sleep(dt - elapsed)

    def _emit_debug(self, debug: GazeDebug) -> None:
        self._last_debug = debug
        if self._on_debug is not None:
            self._on_debug(debug)

    def _emit_follow_state(self) -> None:
        if self._last_debug is None:
            return
        self._emit_debug({**self._last_debug, "follow": self._follow_debug()})

    def _check_follow_watchdog(self) -> None:
        if not self._running or not self.is_following:
            return
        if time.monotonic() - self._last_detection_at <= _FOLLOW_STALE_SECONDS:
            return
        self.stop_follow("camera frame stale")

    def _detector_snapshot(self) -> YoloPoseDetector | None:
        with self._detector_lock:
            return self._detector

    def _follow_debug(self) -> DebugFollow:
        navigation = self._gaze_navigator.snapshot()
        with self._follow_lock:
            goal = navigation.goal
            perception_age_ms = (
                max(0.0, time.monotonic() - self._last_detection_at) * 1000.0 if self._last_detection_at > 0.0 else 0.0
            )
            return {
                "enabled": self._person_follow.is_following,
                "state": str(self._person_follow.state),
                "reference_height": round(self._person_follow.reference_height, 4),
                "observed_height": round(self._person_follow.observed_height, 4),
                "body_center_x": round(self._locked_person.body.center_x, 4) if self._locked_person else None,
                "forward_m": round(self._follow_target.forward_m, 3) if self._follow_target else 0.0,
                "bearing_degrees": (
                    round(math.degrees(self._follow_target.bearing_rad), 1) if self._follow_target else 0.0
                ),
                "perception_age_ms": round(perception_age_ms),
                "nav_state": str(navigation.state),
                "nav_pending": navigation.pending,
                "nav_active": navigation.active,
                "goal": (
                    {
                        "x": round(goal[0], 3),
                        "y": round(goal[1], 3),
                        "yaw_degrees": round(math.degrees(goal[2]), 1),
                    }
                    if goal
                    else None
                ),
                "reason": self._follow_stop_reason or navigation.reason,
            }

    @staticmethod
    def _locked_person_for(people: tuple[PersonPose, ...], result: FaceLockResult) -> PersonPose | None:
        if not result.locked or result.face is None:
            return None
        return next((person for person in people if person.head is result.face), None)

    @staticmethod
    def _status(result: FaceLockResult) -> GazeStatus:
        if result.face is None:
            return GazeStatus.SEARCHING
        if not result.face.centered:
            return GazeStatus.FOLLOWING
        if not result.face.large_enough:
            return GazeStatus.TOO_FAR
        return GazeStatus.LOCKED if result.locked else GazeStatus.CENTERING
