# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import time

import cv2
import numpy as np

from innate import MainImage, Mobility, Skill, SkillReturn, resource

# Must match the dictionary the tags were printed with (ids 0-4, DICT_4X4_50).
# The calibration ChArUco boards use DICT_4X4_250 -- a different dictionary.
ARUCO_DICT = cv2.aruco.DICT_4X4_50
STOP_ID = 4  # seeing this marker ends the follow
LOCK_IDS = {0, 1, 2, 3}

# A single detection is not trusted: background texture occasionally decodes
# as a valid marker for one frame.
LOCK_CONFIRM_FRAMES = 3
STOP_CONFIRM_FRAMES = 2

LOOP_PERIOD = 0.1
CMD_DURATION = 0.4  # cmd_vel deadman: base stops if the loop dies

MAX_LINEAR = 0.3
MAX_REVERSE = 0.1
MAX_ANGULAR = 0.8
TURN_GAIN = 1.5
LINEAR_GAIN = 0.8

# Follow distance is held by keeping the marker's apparent side length at this
# fraction of image width (~0.35 m for an 8 cm tag on the main camera).
TARGET_SIZE_FRAC = 0.16
SIZE_DEADBAND = 0.15

# Smoothing: per-frame corner jitter would otherwise feed straight into the
# command, and step changes in cmd_vel jerk the base.
MEAS_SMOOTHING = 0.35
LINEAR_SLEW = 0.5  # m/s^2
ANGULAR_SLEW = 2.0  # rad/s^2
LOST_GRACE_FRAMES = 5


class FollowAruco(Skill):
    """Follow a person or object carrying an ArUco tag."""

    mobility: Mobility
    # `| None`: the required-feed grace (3 s) is too tight for a cold camera;
    # execute() waits up to 5 s itself.
    image: MainImage | None

    # Per-run tracking state: the class values are the starting point, and
    # each run's instance shadows them as it goes.
    _frame_width = 1
    _offset_filtered: float | None = None
    _size_error_filtered: float | None = None
    _cmd_linear = 0.0
    _cmd_angular = 0.0
    _last_cmd_time: float | None = None

    @resource
    def _detector(self) -> cv2.aruco.ArucoDetector:
        dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
        return cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())

    def guidelines(self) -> str:
        ids = ", ".join(str(i) for i in sorted(LOCK_IDS))
        return (
            f"Follow a person or object carrying an ArUco tag (DICT_4X4_50, ids {ids}). "
            "The robot waits until it sees one, locks onto that id, says 'Locked in', and "
            "then drives to keep that marker centered and about 0.35m away. Showing "
            f"marker id {STOP_ID} stops the follow. Runs until the stop marker is seen or the "
            "skill is cancelled. No obstacle avoidance -- use in clear space."
        )

    def execute(self) -> SkillReturn:
        if self.wait_for(lambda: self.image, timeout=5.0) is None:
            self.fail("No camera image available")
        locked_id = None
        lock_candidate = None
        lock_streak = 0
        stop_streak = 0
        lost_frames = 0
        while True:
            markers = self._detect_markers()

            stop_streak = stop_streak + 1 if STOP_ID in markers else 0
            if stop_streak >= STOP_CONFIRM_FRAMES:
                self._stop()
                self.say("Stopping")
                return f"Stop marker (id {STOP_ID}) seen, follow ended"

            if locked_id is None:
                seen = next((i for i in markers if i in LOCK_IDS), None)
                lock_streak = lock_streak + 1 if seen is not None and seen == lock_candidate else 1
                lock_candidate = seen
                if seen is not None and lock_streak >= LOCK_CONFIRM_FRAMES:
                    locked_id = seen
                    self.say("Locked in")
                    self.feedback(f"Locked onto marker id {locked_id}")
                else:
                    self.sleep(LOOP_PERIOD)
                    continue

            quad = markers.get(locked_id)
            if quad is None:
                lost_frames += 1
                self._offset_filtered = None
                self._size_error_filtered = None
                if lost_frames > LOST_GRACE_FRAMES:
                    self._stop()
                else:
                    self._send_cmd(0.0, 0.0)
            else:
                lost_frames = 0
                self._drive_toward(quad)
            self.sleep(LOOP_PERIOD)

    def _detect_markers(self) -> dict:
        frame = self._current_frame()
        if frame is None:
            return {}
        self._frame_width = frame.shape[1]
        corners, ids, _rejected = self._detector.detectMarkers(frame)
        if ids is None:
            return {}
        return {int(marker_id): quad.reshape(4, 2) for marker_id, quad in zip(ids.flatten(), corners, strict=True)}

    def _current_frame(self):
        frame = self.image
        if not frame:
            return None
        data = np.frombuffer(frame.jpeg, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)

    def _drive_toward(
        self, quad, *, target_center_x=None, target_size_frac=TARGET_SIZE_FRAC, size_deadband=SIZE_DEADBAND
    ) -> None:
        center_x = quad[:, 0].mean()
        center_target = self._frame_width / 2 if target_center_x is None else target_center_x
        offset = (center_x - center_target) / (self._frame_width / 2)
        side_frac = np.mean([np.linalg.norm(quad[i] - quad[(i + 1) % 4]) for i in range(4)]) / self._frame_width
        size_error = 1.0 - side_frac / target_size_frac

        self._offset_filtered = self._smooth(self._offset_filtered, offset)
        self._size_error_filtered = self._smooth(self._size_error_filtered, size_error)

        angular = float(np.clip(-TURN_GAIN * self._offset_filtered, -MAX_ANGULAR, MAX_ANGULAR))

        linear = 0.0
        err = self._size_error_filtered
        if abs(err) > size_deadband:
            past_deadband = err - np.copysign(size_deadband, err)
            linear = float(np.clip(LINEAR_GAIN * past_deadband, -MAX_REVERSE, MAX_LINEAR))

        self._send_cmd(linear, angular)

    @staticmethod
    def _smooth(filtered: float | None, measurement: float) -> float:
        if filtered is None:
            return measurement
        return filtered + MEAS_SMOOTHING * (measurement - filtered)

    def _send_cmd(self, linear, angular) -> None:
        # Slew-limit toward the requested velocities; dt capped at CMD_DURATION
        # because past that the deadman has stopped the base.
        now = time.monotonic()
        dt = min(now - self._last_cmd_time, CMD_DURATION) if self._last_cmd_time is not None else LOOP_PERIOD
        self._last_cmd_time = now
        linear = float(
            np.clip(linear, -getattr(self, "max_reverse", MAX_REVERSE), getattr(self, "max_linear", MAX_LINEAR))
        )
        angular = float(
            np.clip(angular, -getattr(self, "max_angular", MAX_ANGULAR), getattr(self, "max_angular", MAX_ANGULAR))
        )
        linear_slew = getattr(self, "linear_slew", LINEAR_SLEW)
        angular_slew = getattr(self, "angular_slew", ANGULAR_SLEW)
        self._cmd_linear += float(np.clip(linear - self._cmd_linear, -linear_slew * dt, linear_slew * dt))
        self._cmd_angular += float(np.clip(angular - self._cmd_angular, -angular_slew * dt, angular_slew * dt))
        self.mobility.send_cmd_vel(linear_x=self._cmd_linear, angular_z=self._cmd_angular, duration=CMD_DURATION)

    def _stop(self):
        self._offset_filtered = None
        self._size_error_filtered = None
        self._cmd_linear = 0.0
        self._cmd_angular = 0.0
        self._last_cmd_time = None
        self.mobility.stop()
