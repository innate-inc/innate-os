# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Camera capture: the latest main and arm-wrist JPEG frames, plus head pitch.

Owns the on-demand sensor subscriptions created while the brain is active.
Frames arrive JPEG-compressed (sensor_msgs/CompressedImage) and are kept as raw
bytes with an arrival timestamp, so the brain can tell a live feed from a stale
one (a dead camera otherwise serves its last frame forever). The head pitch
(degrees, negative = looking down) is tracked because the pixel->floor
grounding needs the camera angle at frame-capture time.

The last second of frames is kept, not just the newest one: the people engine
analyses a frame off this same stream and names it by its header stamp, and its
boxes may only be drawn on that exact frame (docs/rfc/people-memory.md 7).

It also runs the motion gate on that stream, so the brain wakes for an immediate
turn when the scene changes (someone walks in, waves) instead of waiting out its
idle interval. The gate itself is :mod:`brain_client.perception.motion_gate`,
shared with the people engine so there is one rule and not two.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from geometry_msgs.msg import Twist
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

from brain_client.perception.motion_gate import MotionGate

_DRIVE_SUPPRESS_SEC = 1.5  # after a nonzero cmd_vel: frames lag the command and blur outlasts the stop
_RING_SEC = 1.5  # frame history kept for stamp pairing; the engine ticks at 5 Hz, ~300ms behind
# Only _RING_SEC may decide what the ring holds: 12 frames were 1.5 s at neither
# publish rate (1.47 s at the robot's 7.5 Hz, 1.1 s at the sim's 10 Hz), so a
# late tick found its own frame evicted and the boxes stopped being drawn.
_RING_FRAMES = 24  # hard cap on the ring (~1.2 MB of JPEG)


@dataclass(frozen=True)
class _Frame:
    """One captured frame: ``pitch`` is the head angle when it arrived (the
    grounding geometry) and ``stamp_ns`` its ROS header stamp (the identity
    every other consumer of this stream names it by)."""

    arrival: float  # monotonic
    jpeg: bytes
    pitch: float
    stamp_ns: int


class CameraCapture:
    def __init__(self, node, config):
        self._node = node
        self._config = config
        self._image_sub = None
        self._arm_sub = None
        self._head_sub = None
        self._cmd_vel_sub = None
        # Filled on the executor thread and read on the agent thread: a reader
        # copies it with tuple() — one C call, nothing can interleave — not a lock.
        self._ring: deque[_Frame] = deque(maxlen=_RING_FRAMES)
        self._arm: tuple[float, bytes] | None = None
        self._last_drive = 0.0  # monotonic time of the last nonzero cmd_vel
        self.current_head_pitch = 0.0  # degrees; negative = looking down
        # Motion wiring, set by the composition root: on_motion fires (no args)
        # on sustained scene change; motion_suppressed returns True while the
        # robot is moving itself, so ego-motion never reads as scene motion.
        self.on_motion: Callable[[], None] | None = None
        self.motion_suppressed: Callable[[], bool] = lambda: False
        self._motion = MotionGate()

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
        self._cmd_vel_sub = self._node.create_subscription(Twist, self._config.cmd_vel_topic, self._on_cmd_vel, 10)

    def stop(self) -> None:
        # Destroying here is safe only because brain_client_node is spun
        # single-threaded and stop() runs on that spin thread (between callbacks).
        for sub in (self._image_sub, self._arm_sub, self._head_sub, self._cmd_vel_sub):
            if sub is not None:
                self._node.destroy_subscription(sub)
        self._image_sub = self._arm_sub = self._head_sub = self._cmd_vel_sub = None
        self._ring.clear()
        self._arm = None
        self._motion = MotionGate()  # a restart must not diff against pre-stop frames

    def _on_image(self, msg: CompressedImage) -> None:
        if not msg.data:
            return
        stamp = msg.header.stamp
        frame = _Frame(time.monotonic(), bytes(msg.data), self.current_head_pitch, stamp.sec * 10**9 + stamp.nanosec)
        self._ring.append(frame)
        while len(self._ring) > 1 and frame.arrival - self._ring[0].arrival > _RING_SEC:
            self._ring.popleft()
        if self.on_motion is not None and self._motion.observe(frame.jpeg, frame.pitch, self.motion_suppressed()):
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
        frame = self._fresh_frame(max_age_sec)
        return frame.jpeg if frame is not None else None

    def fresh_frame(self, max_age_sec: float) -> tuple[bytes, float] | None:
        """Newest main frame as (jpeg, head pitch at its arrival), or None if stale."""
        frame = self._fresh_frame(max_age_sec)
        return (frame.jpeg, frame.pitch) if frame is not None else None

    def frame_for_stamp(self, stamp_ns: int, max_age_sec: float) -> tuple[bytes, float] | None:
        """The ring frame with exactly this ROS header stamp, as (jpeg, head
        pitch at its arrival), or None when it has aged out.

        The match is exact on purpose: drawing another consumer's boxes on a
        neighbouring frame would put a name on the wrong person.
        """
        now = time.monotonic()
        for frame in reversed(tuple(self._ring)):
            if frame.stamp_ns != stamp_ns:
                continue
            return (frame.jpeg, frame.pitch) if now - frame.arrival <= max_age_sec else None
        return None

    def fresh_arm_jpeg(self, max_age_sec: float) -> bytes | None:
        return _fresh(self._arm, max_age_sec)

    def _fresh_frame(self, max_age_sec: float) -> _Frame | None:
        ring = tuple(self._ring)
        if not ring:
            return None
        frame = ring[-1]
        return frame if time.monotonic() - frame.arrival <= max_age_sec else None


def _fresh(frame: tuple[float, bytes] | None, max_age_sec: float) -> bytes | None:
    if frame is None or time.monotonic() - frame[0] > max_age_sec:
        return None
    return frame[1]
