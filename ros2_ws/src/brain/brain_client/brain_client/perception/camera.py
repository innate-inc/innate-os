# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Camera capture: the latest main and arm-wrist JPEG frames, plus head pitch.

Owns the on-demand sensor subscriptions created while the brain is active.
Frames arrive JPEG-compressed (sensor_msgs/CompressedImage) and are kept as raw
bytes with an arrival timestamp, so the brain can tell a live feed from a stale
one (a dead camera otherwise serves its last frame forever). The head pitch
(degrees, negative = looking down) is tracked because the pixel->floor
grounding needs the camera angle at frame-capture time.

Also hosts the motion gate: a cheap frame-diff on the main camera that lets the
brain wake for an immediate turn when the scene changes (someone walks in,
waves) instead of waiting out its idle interval.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import cv2
import numpy as np
from geometry_msgs.msg import Twist
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

_MOTION_SAMPLE_SEC = 0.25  # compare at most one frame pair per this interval
_MOTION_PIXEL_DELTA = 25  # gray-level change for a pixel to count as "moved"
_MOTION_MIN_FRACTION = 0.02  # fraction of moved pixels that counts as motion
_MOTION_HOT_SAMPLES = 2  # hot samples within the window before firing
_MOTION_WINDOW = 4  # samples the debounce looks back over (~1s of frames)
_MOTION_COOLDOWN_SEC = 10.0  # minimum gap between fires; sustained motion re-fires at this rate
_MOTION_HEAD_PITCH_EPS = 0.8  # deg between samples; more means the head is moving, not the scene
_DRIVE_SUPPRESS_SEC = 1.5  # after a nonzero cmd_vel: frames lag the command and blur outlasts the stop


class _MotionGate:
    """Fires on scene change while the robot itself is holding still.

    Every _MOTION_SAMPLE_SEC the newest JPEG is decoded at 1/8 linear scale to
    grayscale (~1% of the pixels, sub-ms on the Jetson), blurred, and diffed
    against the previous sample. Firing needs _MOTION_HOT_SAMPLES hot samples
    within the last _MOTION_WINDOW, landing on a hot one: a wave is a short
    burst and one cold sample mid-burst must not reset it (measured on-robot:
    a wave is ~2-3 hot samples over ~0.5-0.75s, sometimes gapped), while a
    single-sample exposure step still never fires. Ego-motion makes every
    pixel "move", so the caller passes ``suppressed`` while the robot drives
    itself (a skill running) and the gate goes cold on its own when the head
    pitch moved between samples. The baseline updates on every sample
    regardless, so coming out of suppression never diffs against an ancient
    frame.
    """

    def __init__(self):
        self._prev: np.ndarray | None = None
        self._prev_pitch = 0.0
        self._last_sample = 0.0
        self._last_fired = 0.0
        self._recent: list[bool] = []  # hot flags of the last few samples
        self.peak_fraction = 0.0  # largest fraction since last consume_peak()

    def consume_peak(self) -> float:
        """Read-and-reset the peak moved fraction (snapshot telemetry)."""
        peak, self.peak_fraction = self.peak_fraction, 0.0
        return peak

    def observe(self, jpeg: bytes, head_pitch: float, suppressed: bool) -> bool:
        now = time.monotonic()
        if now - self._last_sample < _MOTION_SAMPLE_SEC:
            return False
        self._last_sample = now
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
        if frame is None:
            return False
        frame = cv2.GaussianBlur(frame, (5, 5), 0)
        prev, self._prev = self._prev, frame
        head_moved = abs(head_pitch - self._prev_pitch) > _MOTION_HEAD_PITCH_EPS
        self._prev_pitch = head_pitch
        if suppressed or head_moved or prev is None or prev.shape != frame.shape:
            self._recent.clear()
            return False
        moved_fraction = float((cv2.absdiff(prev, frame) > _MOTION_PIXEL_DELTA).mean())
        self.peak_fraction = max(self.peak_fraction, moved_fraction)
        self._recent.append(moved_fraction >= _MOTION_MIN_FRACTION)
        del self._recent[:-_MOTION_WINDOW]
        if not self._recent[-1] or sum(self._recent) < _MOTION_HOT_SAMPLES:
            return False
        if now - self._last_fired < _MOTION_COOLDOWN_SEC:
            return False
        self._last_fired = now
        self._recent.clear()
        return True


class CameraCapture:
    def __init__(self, node, config):
        self._node = node
        self._config = config
        self._image_sub = None
        self._arm_sub = None
        self._head_sub = None
        self._cmd_vel_sub = None
        # (monotonic arrival time, jpeg, head pitch at arrival) — the pitch is
        # stamped with the frame so grounding uses the geometry it was taken at.
        self._image: tuple[float, bytes, float] | None = None
        self._arm: tuple[float, bytes] | None = None
        self._last_drive = 0.0  # monotonic time of the last nonzero cmd_vel
        self.current_head_pitch = 0.0  # degrees; negative = looking down
        # Motion wiring, set by the composition root: on_motion fires (no args)
        # on sustained scene change; motion_suppressed returns True while the
        # robot is moving itself, so ego-motion never reads as scene motion.
        self.on_motion: Callable[[], None] | None = None
        self.motion_suppressed: Callable[[], bool] = lambda: False
        self._motion = _MotionGate()

    def start(self) -> None:
        if self._image_sub is not None:
            return
        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=2,
        )
        self._image_sub = self._node.create_subscription(
            CompressedImage, self._config.image_topic, self._on_image, image_qos
        )
        if self._config.send_arm_camera_image:
            self._arm_sub = self._node.create_subscription(
                CompressedImage, self._config.arm_camera_image_topic, self._on_arm, image_qos
            )
        self._head_sub = self._node.create_subscription(String, "/mars/head/current_position", self._on_head, 10)
        # The base being driven (joystick teleop, nav) is ego-motion the
        # primitive_running check can't see — a manual drive runs no skill.
        self._cmd_vel_sub = self._node.create_subscription(
            Twist, self._config.motion_observation_topic, self._on_cmd_vel, 10
        )

    def stop(self) -> None:
        # Destroying here is safe only because brain_client_node is spun
        # single-threaded and stop() runs on that spin thread (between callbacks).
        for sub in (self._image_sub, self._arm_sub, self._head_sub, self._cmd_vel_sub):
            if sub is not None:
                self._node.destroy_subscription(sub)
        self._image_sub = self._arm_sub = self._head_sub = self._cmd_vel_sub = None
        self._image = self._arm = None
        self._motion = _MotionGate()  # a restart must not diff against pre-stop frames

    def _on_image(self, msg: CompressedImage) -> None:
        if msg.data:
            self._image = (time.monotonic(), bytes(msg.data), self.current_head_pitch)
            if self.on_motion is not None and self._motion.observe(
                self._image[1], self.current_head_pitch, self.motion_suppressed()
            ):
                self.on_motion()

    def _on_arm(self, msg: CompressedImage) -> None:
        if msg.data:
            self._arm = (time.monotonic(), bytes(msg.data))

    def _on_cmd_vel(self, msg: Twist) -> None:
        if any((msg.linear.x, msg.linear.y, msg.angular.z)):
            self._last_drive = time.monotonic()

    @property
    def recently_driven(self) -> bool:
        """A drive command within the last moment: ego-motion, not scene motion."""
        return time.monotonic() - self._last_drive < _DRIVE_SUPPRESS_SEC

    def _on_head(self, msg: String) -> None:
        try:
            self.current_head_pitch = float(json.loads(msg.data)["current_position"])
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            # Keep the last known pitch: one corrupted message must not make the
            # grounding believe the head snapped level (pitch drives pixel->floor).
            pass

    def motion_peak(self) -> float:
        """Largest moved-pixel fraction since the last call (1 Hz snapshot telemetry)."""
        return self._motion.consume_peak()

    def fresh_image_jpeg(self, max_age_sec: float) -> bytes | None:
        return _fresh(self._image, max_age_sec)

    def fresh_frame(self, max_age_sec: float) -> tuple[bytes, float] | None:
        """Newest main frame as (jpeg, head pitch at its arrival), or None if stale."""
        frame = self._image
        if frame is None or time.monotonic() - frame[0] > max_age_sec:
            return None
        return frame[1], frame[2]

    def fresh_arm_jpeg(self, max_age_sec: float) -> bytes | None:
        return _fresh(self._arm, max_age_sec)


def _fresh(frame: tuple[float, bytes] | tuple[float, bytes, float] | None, max_age_sec: float) -> bytes | None:
    if frame is None or time.monotonic() - frame[0] > max_age_sec:
        return None
    return frame[1]
