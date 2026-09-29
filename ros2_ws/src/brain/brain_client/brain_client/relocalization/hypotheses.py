# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pick the robot's pose from the lidar's candidates, or say why it can't. Pure: no ROS, no I/O.

Every distinct pose the global search finds is a hypothesis weighted by its
likelihood score; the robot is localized only when one of them holds nearly all
of the weight and actually explains the scan. Anything less is reported as lost
rather than guessed: a wrong pose seeded into AMCL is worse than none.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_client.relocalization.scan_match import global_search

if TYPE_CHECKING:
    from brain_client.relocalization.scan_match import Grid, Pose2D, Scan

TAU = 0.02  # score gap worth a factor e when the best pose explains the whole scan...
TAU_SLOPE = 0.15  # ...widened per unit of what it leaves unexplained (clutter, a stale map)
MERGE_M = 0.4
MERGE_DEG = 20.0
FIT_MIN = 0.55  # the winner must put this share of endpoints on a wall...
SHARE_MIN = 0.95  # ...and hold this share of the posterior


@dataclass(frozen=True)
class Decision:
    pose: Pose2D | None  # set only when localized
    reason: str


def decide(grid: Grid, scan: Scan) -> Decision:
    """The robot's pose on the map, or why the scan can't pin it down."""
    poses = _distinct(global_search(grid, scan))
    if not poses:
        return Decision(None, "the lidar found no pose that fits the map")
    best = poses[0]
    tau = TAU + TAU_SLOPE * (1.0 - best.score)
    share = 1.0 / sum(math.exp((p.score - best.score) / tau) for p in poses)
    if best.fit < FIT_MIN:
        return Decision(None, f"the scan barely fits the map anywhere (best {best.fit:.0%})")
    if share < SHARE_MIN:
        return Decision(None, f"ambiguous: the best pose holds {share:.0%} of the evidence")
    return Decision(best, f"localized: {share:.0%} of the evidence, lidar fit {best.fit:.0%}")


def _distinct(poses: list[Pose2D]) -> list[Pose2D]:
    """Best first, dropping any pose within MERGE_M and MERGE_DEG of a better one."""
    kept: list[Pose2D] = []
    for pose in sorted(poses, key=lambda p: -p.score):
        if all(pose.distance(k) >= MERGE_M or pose.heading_gap(k) >= math.radians(MERGE_DEG) for k in kept):
            kept.append(pose)
    return kept
