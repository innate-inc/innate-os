# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Holds the base on an expression's stance: the orient (yaw) and advance (x) offsets of a clip.

Pure — no ROS. The control law is the renderer's (``expressive.drive.BaseTracker``); this adds what
a shared, muxed base needs: an anchor captured only when a clip's stance leaves the deadband and
dropped once the robot is back on it, acceleration limits, and silence whenever there is nothing
to correct — offsets inside the deadband (liveliness noise on a gesture that never turns) are zero.
"""

from __future__ import annotations

import math

from brain_client.expressive.drive import BaseTracker
from brain_client.perception.pose import Pose

MAX_WZ = 0.6  # rad/s
MAX_VX = 0.15  # m/s
MAX_DWZ = 4.0  # rad/s², keeps a snappy clip from jerking the chassis
MAX_DVX = 1.0  # m/s²
YAW_DEADBAND = math.radians(1.5)
X_DEADBAND = 0.01  # m
_LAW = BaseTracker(kp_x=3.0, kp_yaw=3.0, max_vx=MAX_VX, max_wz=MAX_WZ, deadband_x=X_DEADBAND, deadband_yaw=YAW_DEADBAND)

Twist2D = tuple[float, float]
"""(linear x m/s, angular z rad/s)."""


class StanceTracker:
    def __init__(self) -> None:
        self._anchor: Pose | None = None
        self._target: tuple[float, float] = (0.0, 0.0)
        self._command: Twist2D = (0.0, 0.0)
        self._moving = False
        self._cut_short = False  # released mid-stance: the next anchor must not replay the offset
        self._abandoned = False  # stopped mid-stance: stay put until the offsets are back to still

    @property
    def anchored(self) -> bool:
        return self._anchor is not None

    def step(self, yaw: float, x: float, pose: Pose | None, dt: float) -> Twist2D | None:
        """The twist to publish for stance offsets ``(yaw rad, x m)``, or None to leave the base alone.

        ``pose`` is the odometry pose, None when it is stale: the base then stops and the anchor is
        dropped, because offsets from it would be meaningless.
        """
        still = abs(yaw) < YAW_DEADBAND and abs(x) < X_DEADBAND
        if still:
            yaw = x = 0.0
        if self._abandoned:
            self._abandoned = not still
            return None
        if self._anchor is None:
            if still or pose is None:
                return None
            self._anchor = _anchor_behind(pose, yaw, x) if self._cut_short else pose
            self._target = (yaw, x)
        if pose is None:
            return self.release()
        feed_yaw, feed_x = ((yaw - self._target[0]) / dt, (x - self._target[1]) / dt) if dt > 0 else (0.0, 0.0)
        self._target = (yaw, x)
        vx, wz = _LAW.twist(self._anchor, pose, x, yaw, feed_x, feed_yaw)
        if vx == 0.0 and wz == 0.0:  # on target and the target is still
            return self.release() if still else self._halt()
        last_vx, last_wz = self._command
        self._command = (
            last_vx + _clamp(vx - last_vx, MAX_DVX * dt),
            last_wz + _clamp(wz - last_wz, MAX_DWZ * dt),
        )
        self._moving = True
        return self._command

    def release(self) -> Twist2D | None:
        """Drop the anchor, to resume from where the robot stands; a final stop if the base was moving."""
        if self._anchor is not None:
            self._cut_short = self._target != (0.0, 0.0)
        self._anchor = None
        return self._halt()

    def abandon(self) -> Twist2D | None:
        """Give the stance up where the robot stands — no trip back to the anchor — and ignore the
        offsets until they are still again; a final stop if the base was moving."""
        self._abandoned = self._anchor is not None or self._abandoned
        self._anchor = None
        self._cut_short = False
        return self._halt()

    def _halt(self) -> Twist2D | None:
        """One zero twist, then silence: a held zero would keep the mux's skills input claimed."""
        self._command = (0.0, 0.0)
        if not self._moving:
            return None
        self._moving = False
        return self._command


def _anchor_behind(pose: Pose, yaw: float, x: float) -> Pose:
    """The anchor from which ``pose`` already sits at offsets ``(yaw, x)``, for resuming a stance a
    skill or the joystick cut short without replaying its offset from where the robot now stands."""
    px, py, heading = pose
    anchor_yaw = heading - yaw
    return (px - x * math.cos(anchor_yaw), py - x * math.sin(anchor_yaw), anchor_yaw)


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))
