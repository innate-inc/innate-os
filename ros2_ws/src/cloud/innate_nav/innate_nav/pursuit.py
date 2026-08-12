#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Robot-side plan tracking: re-anchoring, pure pursuit, blocked detection.

Pure geometry over the metres the server already denormalized — no torch, no
model constants. The lookahead and turn threshold below are *control* tuning,
deliberately owned by the robot; the contract constants (the 2 m / pi scales,
the collapse threshold) never leave the server.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from .policy_client import Plan, Pose


@dataclass(frozen=True)
class PursuitCfg:
    lookahead_m: float = 0.75       # ControllerCfg.lookahead_m
    turn_threshold_deg: float = 22.5  # ControllerCfg.turn_threshold_deg
    v_max: float = 0.4              # innate-os world.MAX_LINEAR
    w_max: float = 1.0              # innate-os world.MAX_YAW
    k_yaw: float = 1.5
    # How far the steering target may be pushed sideways to clear an obstacle.
    # This is what widens a corner the policy shaved; beyond ~0.2 m it stops
    # tracking the policy's intent and starts inventing its own route.
    avoid_shift_m: float = 0.18
    # Inflation cost at which forward speed reaches its floor. Nav2's local
    # inflation_radius is 0.25 m and INSCRIBED is 99.
    slow_cost: float = 70.0
    # Speed never scales below this while a safe prefix exists. safe_length is
    # the hard veto; if the soft term could also reach zero the two conspire
    # into a permanent freeze -- which is exactly what happened, because
    # truncate_to puts the cut point ON the first blocked cell, so the worst
    # cost of the kept path was always ~INSCRIBED and v was always 0.
    min_speed_scale: float = 0.35


def reanchor(waypoints_m: np.ndarray, capture: Pose, now: Pose) -> np.ndarray:
    """Re-express a plan from the robot frame AT CAPTURE into the robot frame
    NOW, through the odometry delta.

    Plans arrive 500-600 ms old (frame age + compute + transport). At 0.4 m/s
    that is a quarter metre of stale origin; tracking them as if they were
    current puts a systematic 0.1-0.3 m bias into every path.
    """
    wp = np.asarray(waypoints_m, dtype=np.float64).reshape(-1, 3).copy()
    dyaw = capture.yaw - now.yaw
    c, s = math.cos(dyaw), math.sin(dyaw)
    # capture-frame point -> world -> current frame, composed into one rotation
    # of the point plus the rotated translation offset.
    dx, dy = capture.x - now.x, capture.y - now.y
    ox = math.cos(now.yaw) * dx + math.sin(now.yaw) * dy
    oy = -math.sin(now.yaw) * dx + math.cos(now.yaw) * dy
    out = np.empty_like(wp)
    out[:, 0] = c * wp[:, 0] - s * wp[:, 1] + ox
    out[:, 1] = s * wp[:, 0] + c * wp[:, 1] + oy
    out[:, 2] = wp[:, 2] + dyaw
    return out


def steering_target(wp_m: np.ndarray, lookahead_m: float) -> np.ndarray:
    """First waypoint at cumulative path length >= lookahead (else the
    farthest). Cumulative rather than straight-line so a curving path is
    followed instead of short-cut. Mirrors controller.steering_target, which
    this package cannot import — that module lives behind torch."""
    seg = np.linalg.norm(np.diff(wp_m[:, :2], axis=0, prepend=0.0), axis=1)
    cum = np.cumsum(seg)
    idx = int(np.argmax(cum >= lookahead_m)) if (cum >= lookahead_m).any() else len(wp_m) - 1
    return wp_m[idx]


def velocity(wp_m: np.ndarray, cfg: PursuitCfg = PursuitCfg()) -> tuple[float, float]:
    """(v, omega) for the base. Above the turn threshold the robot turns in
    place: training only ever saw pure translations and pure rotations on the
    discrete action grid, so keeping gross motion to one or the other keeps the
    observed frame sequence closer to the distribution the model expects."""
    tgt = steering_target(wp_m, cfg.lookahead_m)
    bearing = math.atan2(tgt[1], tgt[0])
    w = max(-cfg.w_max, min(cfg.w_max, cfg.k_yaw * bearing))
    if abs(math.degrees(bearing)) > cfg.turn_threshold_deg:
        return 0.0, w
    return cfg.v_max * max(0.0, math.cos(bearing)), w


def to_odom(wp_m: np.ndarray, at: Pose) -> np.ndarray:
    """Body-frame waypoints -> odom xy, for costmap lookups and /nav_policy/path."""
    wp = np.asarray(wp_m, dtype=np.float64).reshape(-1, 3)
    c, s = math.cos(at.yaw), math.sin(at.yaw)
    return np.column_stack([at.x + c * wp[:, 0] - s * wp[:, 1],
                            at.y + s * wp[:, 0] + c * wp[:, 1]])


def odom_path_from(wp_m: np.ndarray, at: Pose) -> np.ndarray:
    """Odom xy path that STARTS at the robot, then the waypoints.

    Arc length has to be measured from the robot, because that is the
    convention steering_target and truncate_to use (their cumulative sum
    prepends the origin). Measuring from the first waypoint instead would
    silently shift every clearance decision by the 0.25 m leg in between.
    """
    return np.vstack([[at.x, at.y], to_odom(wp_m, at)])


def truncate_to(wp_m: np.ndarray, safe_len_m: float) -> np.ndarray:
    """The path prefix within `safe_len_m` of arc length, with the cut point
    interpolated so the last target sits exactly at the limit rather than
    snapping back to the previous waypoint."""
    wp = np.asarray(wp_m, dtype=np.float64).reshape(-1, 3)
    if safe_len_m <= 0.0:
        return wp[:0]
    seg = np.linalg.norm(np.diff(wp[:, :2], axis=0, prepend=0.0), axis=1)
    cum = np.cumsum(seg)
    keep = int(np.searchsorted(cum, safe_len_m))
    if keep >= len(wp):
        return wp
    prev_len = cum[keep - 1] if keep else 0.0
    span = cum[keep] - prev_len
    if span < 1e-9:
        return wp[:keep]
    t = (safe_len_m - prev_len) / span
    base = wp[keep - 1] if keep else np.zeros(3)
    cut = base + t * (wp[keep] - base)
    return np.vstack([wp[:keep], cut])


def avoid(target: np.ndarray, repulse_body: np.ndarray,
          cfg: PursuitCfg = PursuitCfg()) -> np.ndarray:
    """Push the steering target sideways, away from obstacles.

    Only the component perpendicular to the target bearing is applied: pushing
    along the bearing would just brake or lengthen the lookahead, and speed is
    already handled separately. The perpendicular part is what makes the robot
    swing wide around a corner instead of clipping it.
    """
    tgt = np.asarray(target, dtype=np.float64).copy()
    rep = np.asarray(repulse_body, dtype=np.float64).reshape(2)
    norm = float(np.hypot(*rep))
    if norm < 1e-9:
        return tgt
    heading = tgt[:2] / max(float(np.hypot(*tgt[:2])), 1e-9)
    perp = np.array([-heading[1], heading[0]])
    tgt[:2] = tgt[:2] + perp * float(np.dot(rep, perp)) * cfg.avoid_shift_m
    return tgt


def speed_scale(worst_cost: float, cfg: PursuitCfg = PursuitCfg()) -> float:
    """1.0 in the open, tapering to `min_speed_scale` inside the inflation band.

    Deliberately never returns 0: whether the robot may move at all is
    safe_length's decision, and a soft term that can also veto turns "close to
    something" into "stuck forever".
    """
    if worst_cost <= 0.0:
        return 1.0
    t = min(1.0, float(worst_cost) / cfg.slow_cost)
    return float(1.0 - t * (1.0 - cfg.min_speed_scale))


class BlockedDetector:
    """Wedged-against-a-wall detection from odometry.

    The camera cannot see this: pressed into a wall the view is a texture
    close-up, far off-distribution, and the model keeps confidently predicting
    forward motion. Commanded-versus-actual displacement is the only honest
    signal. The sim eval lost a large fraction of its episodes to exactly this
    before detection existed.
    """

    def __init__(self, window_s: float = 1.5, min_ratio: float = 0.25):
        self.window_s = window_s
        self.min_ratio = min_ratio
        self._hist: deque[tuple[float, float, Pose]] = deque()  # (t, v_cmd, pose)

    def update(self, t: float, v_cmd: float, pose: Pose) -> bool:
        self._hist.append((t, v_cmd, pose))
        while self._hist and t - self._hist[0][0] > self.window_s:
            self._hist.popleft()
        if len(self._hist) < 2:
            return False
        hist = list(self._hist)
        expected = sum(abs(v_cmd) * (t_next - t_cur)
                       for (t_cur, v_cmd, _), (t_next, _, _) in zip(hist, hist[1:], strict=False))
        if expected < 0.05:            # barely commanded to move: not blocked
            return False
        actual = math.hypot(pose.x - hist[0][2].x, pose.y - hist[0][2].y)
        return actual < self.min_ratio * expected

    def reset(self) -> None:
        self._hist.clear()


def recovery_turn(wp_m: np.ndarray, cfg: PursuitCfg = PursuitCfg()) -> float:
    """Which way to spin when blocked: toward the bearing of the waypoint that
    reaches furthest from the robot — the model's own best guess at open space,
    even when its forward plan is unexecutable."""
    furthest = wp_m[int(np.argmax(np.linalg.norm(wp_m[:, :2], axis=1)))]
    bearing = math.atan2(furthest[1], furthest[0])
    return cfg.w_max if bearing >= 0 else -cfg.w_max


def plan_waypoints(plan: Plan) -> np.ndarray:
    return np.asarray(plan.waypoints_m, dtype=np.float64).reshape(-1, 3)
