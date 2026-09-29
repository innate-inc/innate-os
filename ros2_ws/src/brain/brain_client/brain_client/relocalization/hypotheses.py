# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Fuse place recognition with lidar into one pose decision. Pure: no ROS, no I/O.

Every candidate pose ("mode") comes from the lidar — the global search plus
the best poses in and around the places the vision model named. A mode's
weight is its visual prior times its lidar likelihood; the robot is localized
only when one mode holds most of the weight, is a place the vision model
named, and actually explains the scan. Global lidar modes always compete, so
a place that looks right but that the lidar contradicts never wins, and a
lidar alias the vision model never named never wins either — unless the camera
recognized nothing at all and the lidar alone is unambiguous.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import cv2
import numpy as np

from brain_client.relocalization.scan_match import Pose2D, evaluate, global_search, refine, region_search, view_region

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brain_client.relocalization.scan_match import Grid, Scan

Xyt = tuple[float, float, float]

TAU = 0.02  # score gap worth a factor e when the best pose explains the whole scan...
TAU_SLOPE = 0.15  # ...widened per unit of what it leaves unexplained (clutter, a stale map)
EPS = 0.05  # visual prior of a place the vision model did not name
DECAY_M = 1.5  # a named place's support fades over this distance outside its view region
SEARCH_PAD_M = 1.5  # region searches reach this far past the named regions
HEADING_FREE_DEG = 60.0  # a view matching frame F points within this of F's heading...
HEADING_DECAY_DEG = 30.0  # ...and fades beyond it
MERGE_M = 0.4
MERGE_DEG = 20.0
FIT_MIN = 0.55  # the winner must put this share of endpoints on a wall
SHARE_MIN = 0.85  # ...hold this share of the posterior...
SUPPORT_MIN = 0.5  # ...and be a place the vision model named this confidently
LIDAR_ALONE_MIN = 0.95  # when the camera recognized nothing, the lidar share that suffices on its own


@dataclass(frozen=True)
class Look:
    """One stop of the search: what the robot saw, and where odometry says it was."""

    scan: Scan
    odom: Xyt


@dataclass(frozen=True)
class Match:
    """The vision model's claim that a look shows the place a remembered frame shows."""

    look: int
    frame: int
    frame_pose: Xyt  # map pose the frame was recorded from
    confidence: float


@dataclass
class Mode:
    pose: Pose2D  # the robot now, map frame, fitted to the current scan
    support: float = 0.0
    frames: set[int] = field(default_factory=set)
    share: float = 0.0


@dataclass(frozen=True)
class Decision:
    pose: Pose2D | None  # set only when localized
    reason: str
    modes: tuple[Mode, ...]


def compose(a: Xyt, b: Xyt) -> Xyt:
    ax, ay, at = a
    bx, by, bt = b
    return (ax + bx * math.cos(at) - by * math.sin(at), ay + bx * math.sin(at) + by * math.cos(at), at + bt)


def relative(frm: Xyt, to: Xyt) -> Xyt:
    """``to`` expressed in ``frm``'s frame."""
    fx, fy, ft = frm
    dx, dy = to[0] - fx, to[1] - fy
    return (dx * math.cos(ft) + dy * math.sin(ft), -dx * math.sin(ft) + dy * math.cos(ft), to[2] - ft)


def decide(grid: Grid, looks: Sequence[Look], matches: Sequence[Match]) -> Decision:
    """The pose of the robot at the last look, or why it can't be trusted yet."""
    regions = {m.frame: view_region(grid, *m.frame_pose) for m in matches}
    modes = _weigh(_support(grid, looks, matches, regions, _modes(grid, looks, matches, regions)))
    if not modes:
        return Decision(None, "the lidar found no pose that fits the map", ())
    if all(m.confidence < SUPPORT_MIN for m in matches):
        alone = _lidar_alone(modes)
        if alone is not None:
            return Decision(
                alone, "localized by the lidar alone (the camera recognized nothing it could contradict)", tuple(modes)
            )
    win = modes[0]
    if win.support < SUPPORT_MIN:
        reason = (
            "the lidar prefers a place the camera did not recognize"
            if win.support <= EPS
            else "the camera is not sure enough about this place"
        )
        return Decision(None, reason, tuple(modes))
    if win.pose.fit < FIT_MIN:
        return Decision(None, f"the lidar barely fits the recognized place ({win.pose.fit:.0%})", tuple(modes))
    if win.share < SHARE_MIN:
        return Decision(None, f"ambiguous: the best pose holds {win.share:.0%} of the evidence", tuple(modes))
    return Decision(win.pose, "localized", tuple(modes))


def _modes(grid: Grid, looks: Sequence[Look], matches: Sequence[Match], regions: dict[int, np.ndarray]) -> list[Mode]:
    now = looks[-1]
    found = list(global_search(grid, now.scan))
    for i, look in enumerate(looks):
        seeds = [] if look is now else global_search(grid, look.scan, k=4)
        named = [regions[m.frame] for m in matches if m.look == i]
        if named:
            near = cv2.dilate(
                np.logical_or.reduce(named).astype(np.uint8),
                np.ones((3, 3), np.uint8),
                iterations=int(SEARCH_PAD_M / grid.resolution),
            )
            seeds += region_search(grid, look.scan, near.astype(bool))
        for p in seeds:
            x, y, t = compose((p.x, p.y, p.theta), relative(look.odom, now.odom))
            found.append(refine(grid, now.scan, Pose2D(x, y, t), span_m=0.1, span_deg=4.0))
    merged: list[Mode] = []
    for pose in sorted(found, key=lambda p: -p.score):
        if all(pose.distance(m.pose) >= MERGE_M or pose.heading_gap(m.pose) >= math.radians(MERGE_DEG) for m in merged):
            merged.append(Mode(_explain_all(grid, looks, pose)))
    return merged


def _explain_all(grid: Grid, looks: Sequence[Look], pose: Pose2D) -> Pose2D:
    """Score a pose by how well it explains every look's scan, each carried back by
    odometry — one scan can fit a look-alike room; a sequence of them rarely does."""
    now = looks[-1].odom
    fits = [
        evaluate(grid, look.scan, *compose((pose.x, pose.y, pose.theta), relative(now, look.odom))) for look in looks
    ]
    return Pose2D(
        pose.x, pose.y, pose.theta, sum(f.fit for f in fits) / len(fits), sum(f.score for f in fits) / len(fits)
    )


def _support(
    grid: Grid, looks: Sequence[Look], matches: Sequence[Match], regions: dict[int, np.ndarray], modes: list[Mode]
) -> list[Mode]:
    """Each match supports a mode by its confidence, fading with the mode's distance
    outside the frame's view region and its heading's turn away from the frame's;
    supports combine by max within a look and noisy-OR across looks."""
    now = looks[-1]
    outside = {
        frame: cv2.distanceTransform((~region).astype(np.uint8), cv2.DIST_L2, 5) * grid.resolution
        for frame, region in regions.items()
    }
    for mode in modes:
        per_look: dict[int, float] = {}
        for m in matches:
            x, y, t = compose((mode.pose.x, mode.pose.y, mode.pose.theta), relative(now.odom, looks[m.look].odom))
            cell = grid.cell(x, y)
            if cell is None:
                continue
            frame_theta = m.frame_pose[2]
            turn = abs(math.degrees(math.atan2(math.sin(t - frame_theta), math.cos(t - frame_theta))))
            support = (
                m.confidence
                * math.exp(-max(0.0, turn - HEADING_FREE_DEG) / HEADING_DECAY_DEG)
                * math.exp(-float(outside[m.frame][cell]) / DECAY_M)
            )
            per_look[m.look] = max(per_look.get(m.look, 0.0), support)
            if support > 0.2 * m.confidence:
                mode.frames.add(m.frame)
        mode.support = 1.0 - math.prod(1.0 - s for s in per_look.values())
    return modes


def _lidar_alone(modes: list[Mode]) -> Pose2D | None:
    """The lidar's own winner when it is unambiguous with no visual prior at all."""
    best = max(m.pose.score for m in modes)
    tau = _tau(best)
    weights = [math.exp((m.pose.score - best) / tau) for m in modes]
    i = max(range(len(modes)), key=lambda j: weights[j])
    pose = modes[i].pose
    return pose if weights[i] / sum(weights) >= LIDAR_ALONE_MIN and pose.fit >= FIT_MIN else None


def _tau(best_score: float) -> float:
    return TAU + TAU_SLOPE * (1.0 - best_score)


def _weigh(modes: list[Mode]) -> list[Mode]:
    if not modes:
        return modes
    best = max(m.pose.score for m in modes)
    tau = _tau(best)
    weights = [max(m.support, EPS) * math.exp((m.pose.score - best) / tau) for m in modes]
    total = sum(weights)
    for mode, weight in zip(modes, weights, strict=True):
        mode.share = weight / total
    return sorted(modes, key=lambda m: -m.share)
