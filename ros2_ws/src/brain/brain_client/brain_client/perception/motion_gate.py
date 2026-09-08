# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The motion gate: has the scene changed while the robot itself held still?

One rule, two readers. The brain runs it on its head-camera ring to wake a turn
when someone walks in; the people engine runs it on its own frame stream to lift
its duty cycle out of idle (docs/rfc/people-memory.md 4.6, which asks for the
same rule rather than a second gate).

PURE module: cv2, numpy and the monotonic clock. No ROS, no I/O.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

MOTION_SAMPLE_SEC = 0.25  # compare at most one frame pair per this interval
MOTION_PIXEL_DELTA = 25  # gray-level change for a pixel to count as "moved"
MOTION_MIN_FRACTION = 0.02  # fraction of moved pixels that counts as motion
MOTION_HOT_SAMPLES = 2  # hot samples within the window before firing
MOTION_WINDOW = 4  # samples the debounce looks back over (~1s of frames)
MOTION_COOLDOWN_SEC = 10.0  # minimum gap between fires; sustained motion re-fires at this rate
MOTION_HEAD_PITCH_EPS = 0.8  # deg between samples; more means the head is moving, not the scene


class MotionGate:
    """Fires on scene change while the robot itself is holding still.

    Every MOTION_SAMPLE_SEC the newest JPEG is decoded at 1/8 linear scale to
    grayscale (~1% of the pixels, sub-ms on the Jetson), blurred, and diffed
    against the previous sample. Firing needs MOTION_HOT_SAMPLES hot samples
    within the last MOTION_WINDOW, landing on a hot one: a wave is a short
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
        if now - self._last_sample < MOTION_SAMPLE_SEC:
            return False
        self._last_sample = now
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
        if frame is None:
            return False
        frame = cv2.GaussianBlur(frame, (5, 5), 0)
        prev, self._prev = self._prev, frame
        head_moved = abs(head_pitch - self._prev_pitch) > MOTION_HEAD_PITCH_EPS
        self._prev_pitch = head_pitch
        if suppressed or head_moved or prev is None or prev.shape != frame.shape:
            self._recent.clear()
            return False
        moved_fraction = float((cv2.absdiff(prev, frame) > MOTION_PIXEL_DELTA).mean())
        self.peak_fraction = max(self.peak_fraction, moved_fraction)
        self._recent.append(moved_fraction >= MOTION_MIN_FRACTION)
        del self._recent[:-MOTION_WINDOW]
        if not self._recent[-1] or sum(self._recent) < MOTION_HOT_SAMPLES:
            return False
        if now - self._last_fired < MOTION_COOLDOWN_SEC:
            return False
        self._last_fired = now
        self._recent.clear()
        return True
