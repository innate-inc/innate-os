# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Stall detection: the wheels claim motion that the lidar shows did not happen, so the robot is pushing
against something below the lidar while odometry counts distance that was never driven. Pure: the node
feeds it scans and wheel odometry and acts on the verdicts (cancel navigation, put AMCL back)."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from mars_nav.scan_match import MIN_FIT, MIN_SCAN_POINTS, Grid, Pose2D, Scan, endpoint_distances, evaluate

if TYPE_CHECKING:
    from geometry_msgs.msg import PoseWithCovarianceStamped
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import LaserScan

SCAN_HISTORY = 32  # ~4 s of /scan_fast
ODOM_HISTORY = 120  # ~4 s of /odom at 30 Hz
ODOM_MATCH_S = 0.1
LOOKBACK_S = 3.0  # how far back a check looks for a scan the wheels claim to have left
MIN_MOVE_M = 0.08  # wheel motion smaller than this, and than MIN_TURN, the lidar cannot judge
MIN_TURN = math.radians(8)
RANGE_M = 4.0  # nearby structure shows motion; far returns only slow the check
GRID_M = 0.02
INLIER_M = 0.03  # tight enough to tell a few centimetres of motion from none
MARGIN = 0.25  # "stood still" must fit this much more of the scan than the wheels' claimed motion
STRIKES = 3  # consecutive scans
QUIET_S = 3.0  # the base needs this long to stop before a stall can be judged again
SECTORS = 8
MIN_SECTORS = 3  # the unchanged points must span this many sectors: a person walking alongside fills one, walls several


@dataclass(frozen=True)
class Stall:
    claimed: Pose2D  # the wheels' claimed motion, in the earlier scan's frame
    still_fit: float  # share of the new scan unchanged from the earlier one
    moved_fit: float  # share of it found where the wheels claim it went
    window_s: float


def stamp(msg: LaserScan | Odometry | PoseWithCovarianceStamped) -> float:
    return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9


class StallDetector:
    def __init__(self) -> None:
        self._scans: deque[LaserScan] = deque(maxlen=SCAN_HISTORY)
        self._odom: deque[tuple[float, Pose2D]] = deque(maxlen=ODOM_HISTORY)
        self._odom_received_at = 0.0
        self._strikes = 0
        self._quiet_until = 0.0

    def reset(self) -> None:
        self._scans.clear()
        self._odom.clear()
        self._strikes = 0
        self._quiet_until = 0.0

    def on_odom(self, msg: Odometry) -> None:
        pose = msg.pose.pose
        theta = 2.0 * math.atan2(pose.orientation.z, pose.orientation.w)
        self._odom.append((stamp(msg), Pose2D(pose.position.x, pose.position.y, theta)))
        self._odom_received_at = time.monotonic()

    def on_scan(self, msg: LaserScan) -> Stall | None:
        """Judge the newest scan: a Stall once STRIKES scans in a row show the robot standing still where
        the wheels claim it moved. Quiet for QUIET_S afterwards, so the base has time to stop."""
        self._scans.append(msg)
        if time.monotonic() < self._quiet_until:
            return None
        claim = self._wheel_claim()
        if claim is None:
            self._strikes = 0
            return None
        before, after, claimed = claim
        earlier, now = Scan.from_laser_scan(before, RANGE_M), Scan.from_laser_scan(after, RANGE_M)
        if min(len(earlier), len(now)) < MIN_SCAN_POINTS:
            self._strikes = 0
            return None
        field = Grid.from_scan(earlier, GRID_M)
        unchanged = endpoint_distances(field, now, 0.0, 0.0, 0.0) < INLIER_M
        still_fit = float(unchanged.mean())
        moved = evaluate(field, now, claimed.x, claimed.y, claimed.theta, INLIER_M)
        if still_fit < MIN_FIT or still_fit < moved.fit + MARGIN or _sectors(now, unchanged) < MIN_SECTORS:
            self._strikes = 0
            return None
        self._strikes += 1
        if self._strikes < STRIKES:
            return None
        self._strikes = 0
        self._quiet_until = time.monotonic() + QUIET_S
        return Stall(claimed, still_fit, moved.fit, stamp(after) - stamp(before))

    def wheels_turning(self, window_s: float) -> bool:
        """Whether wheel odometry moved within the last window_s; True too while odometry cannot tell,
        because it stopped arriving or the buffer is younger than the window."""
        if not self._odom or time.monotonic() - self._odom_received_at > window_s:
            return True
        latest_at, latest = self._odom[-1]
        for at, pose in reversed(self._odom):
            if latest_at - at > window_s:
                return False
            if pose.distance(latest) > 0.005 or pose.heading_gap(latest) > 0.005:
                return True
        return True

    def _wheel_claim(self) -> tuple[LaserScan, LaserScan, Pose2D] | None:
        """The newest scan, the latest earlier one the wheels claim to have clearly moved away from,
        and that claimed motion in the earlier scan's frame; None when the wheels claim no such move."""
        if len(self._scans) < 2 or not self._odom or self._odom[0][1] == self._odom[-1][1]:
            return None  # parked wheels report the same pose for the whole buffer
        after = self._scans[-1]
        end = self._odom_at(stamp(after))
        if end is None:
            return None
        for before in reversed(list(self._scans)[:-1]):
            if stamp(after) - stamp(before) > LOOKBACK_S:
                return None
            start = self._odom_at(stamp(before))
            if start is None:
                continue
            claimed = end.relative_to(start)
            if math.hypot(claimed.x, claimed.y) >= MIN_MOVE_M or abs(claimed.theta) >= MIN_TURN:
                return before, after, claimed
        return None

    def _odom_at(self, at: float) -> Pose2D | None:
        if not self._odom:
            return None
        nearest, pose = min(self._odom, key=lambda sample: abs(sample[0] - at))
        return pose if abs(nearest - at) <= ODOM_MATCH_S else None


def _sectors(scan: Scan, points: np.ndarray) -> int:
    """How many of SECTORS equal bearing sectors around the robot the selected endpoints fall in."""
    bearings = np.arctan2(scan.py[points], scan.px[points])
    return len(np.unique(((bearings + math.pi) * SECTORS / (2 * math.pi)).astype(int) % SECTORS))
