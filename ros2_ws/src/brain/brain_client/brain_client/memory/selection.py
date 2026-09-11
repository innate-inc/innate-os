# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Which viewpoints earn a slot in the spatial memory. Pure: no ROS, no I/O.

A memory earns its place by showing floor the kept frames don't: with a grid,
novelty is visibility paint (memory/coverage.py) — record while the wedge in
front of the camera is under the coverage threshold, and overlap is welcome
(rather too many pictures than too few). The demand scales with the view: a
roomy wedge tolerates overlap, while a cramped one (nose to a wall) must be
mostly new — important things hang on walls, so a close-up of an unknown
wall earns a slot, but sliding along a known one does not. Without a grid,
pose redundancy stands in: two viewpoints are interchangeable only when both
close AND similarly oriented. Capacity is bounded; at the cap, the memory
whose paint is most replaceable makes room, and the further-back shot outlives
the close-ups it subsumes. Paint also ages: a wedge painted long ago no longer
vouches for the scene, so a stale area records anew — from whatever angle the
robot happens to look — while the old shot keeps its slot until eviction.

A kept viewpoint's picture refreshes only from a frame that shows the same
information, and at most every ten seconds. Heading is what decides that: two
frames aimed the same way, displaced along that very axis, with nothing but
free space between their capture points show the same scene — one merely a
step closer. So refresh needs a near-identical heading, a displacement riding
the view axis (a lateral step slides different information into frame), and a
clear line of sight on the occupancy grid (without a grid, a tight distance
stands in). An oblique, mid-turn, side-stepped, or behind-a-wall take never
overwrites a straight-on view — it becomes a new viewpoint instead, once
enough of its wedge is unpainted.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_client.memory.coverage import OCCUPIED_THRESHOLD
from brain_client.memory.store import Memory
from brain_client.state.map import Map

if TYPE_CHECKING:
    from brain_client.memory.coverage import Coverage

MAX_MEMORIES = 50
_REDUNDANT_DISTANCE_M = 1.0
_REDUNDANT_ANGLE_RAD = math.radians(100.0)  # most of the camera's 128 deg FOV still overlaps within this
_SAME_VIEW_DISTANCE_M = 0.3  # lateral drift bound off the view axis, and the gridless fallback radius
_SAME_VIEW_ANGLE_RAD = math.radians(20.0)
_REFRESH_AGE_SEC = 10.0  # dwelling refreshes the picture this often; the old frame is deleted, so churn stays cheap
_COVERAGE_THRESHOLD = 0.8  # a roomy view records until this much of its wedge is painted — overlap is welcome
_WIDE_VIEW_M2 = 1.0  # below this the view is a close-up
_CLOSEUP_THRESHOLD = 0.28  # a close-up must be mostly new — 0.2 starved small rooms, where every view is cramped


@dataclass(frozen=True)
class Admission:
    """What to do with a candidate viewpoint; replace/evict name existing memories."""

    record: bool
    replace: Memory | None = None  # same view, aged picture: overwrite in place
    evict: Memory | None = None  # at capacity: remove this one to make room


_SKIP = Admission(record=False)


def plan_admission(
    memories: Sequence[Memory],
    x: float,
    y: float,
    theta: float,
    stamp: float,
    grid: Map | None = None,
    coverage: Coverage | None = None,
) -> Admission:
    nearest = min(memories, key=lambda m: redundancy(m, x, y, theta), default=None)
    if (
        nearest is not None
        and redundancy(nearest, x, y, theta) < 1.0
        and stamp - nearest.stamp >= _REFRESH_AGE_SEC
        and _same_view(nearest, x, y, theta, grid)
    ):
        return Admission(record=True, replace=nearest)
    if not _worth_a_slot(memories, x, y, theta, stamp, grid, coverage, nearest):
        return _SKIP
    if len(memories) < MAX_MEMORIES:
        return Admission(record=True)
    evict = coverage.least_unique(memories, grid) if coverage is not None and grid is not None else None
    return Admission(record=True, evict=evict if evict is not None else _most_redundant(memories))


def _worth_a_slot(
    memories: Sequence[Memory],
    x: float,
    y: float,
    theta: float,
    stamp: float,
    grid: Map | None,
    coverage: Coverage | None,
    nearest: Memory | None,
) -> bool:
    """Novelty: with a grid, whether enough of the wedge ahead is unpainted —
    the smaller the view, the newer it must be; without one, pose redundancy."""
    if coverage is not None and grid is not None:
        view = coverage.assess(memories, grid, x, y, theta, stamp)
        if view is not None:
            threshold = _COVERAGE_THRESHOLD if view.visible_m2 >= _WIDE_VIEW_M2 else _CLOSEUP_THRESHOLD
            return view.painted_fraction < threshold
    return nearest is None or redundancy(nearest, x, y, theta) >= 1.0


def _same_view(memory: Memory, x: float, y: float, theta: float, grid: Map | None) -> bool:
    """The same picture — only such a frame may overwrite the stored one.
    Heading decides; the displacement may only ride the view axis (ahead or
    behind — a lateral step frames different information), and the sight line
    must be clear. Distance is capped implicitly: the caller consults this
    inside the redundancy disc, so a same-heading frame beyond
    _REDUNDANT_DISTANCE_M is a new viewpoint, never a refresh."""
    if _angle_diff(memory.theta, theta) >= _SAME_VIEW_ANGLE_RAD:
        return False
    dx, dy = x - memory.x, y - memory.y
    if abs(dy * math.cos(memory.theta) - dx * math.sin(memory.theta)) > _SAME_VIEW_DISTANCE_M:
        return False
    if grid is None:
        return math.hypot(dx, dy) <= _SAME_VIEW_DISTANCE_M
    return _clear_line(grid, memory.x, memory.y, x, y)


def _clear_line(grid: Map, ax: float, ay: float, bx: float, by: float) -> bool:
    """Nothing but known-free cells on the segment — the two endpoints see the
    same scene. Unknown or out-of-map counts as blocked: what the map cannot
    vouch for must not justify an overwrite."""
    cells = grid.grid
    if cells is None:
        return False
    steps = max(1, math.ceil(math.hypot(bx - ax, by - ay) / (grid.resolution / 2)))
    for i in range(steps + 1):
        t = i / steps
        col = int((ax + (bx - ax) * t - grid.origin_x) / grid.resolution)
        row = int((ay + (by - ay) * t - grid.origin_y) / grid.resolution)
        if not (0 <= row < grid.height and 0 <= col < grid.width):
            return False
        value = int(cells[row, col])
        if value < 0 or value >= OCCUPIED_THRESHOLD:
            return False
    return True


def redundancy(memory: Memory, x: float, y: float, theta: float) -> float:
    """How interchangeable a memory is with this viewpoint; < 1 means redundant.

    The max of the normalized position and heading gaps: both must be small
    for the views to overlap, so either being large keeps the pair distinct.
    """
    position = math.hypot(memory.x - x, memory.y - y) / _REDUNDANT_DISTANCE_M
    heading = _angle_diff(memory.theta, theta) / _REDUNDANT_ANGLE_RAD
    return max(position, heading)


def _most_redundant(memories: Sequence[Memory]) -> Memory:
    """The older half of the closest pair — evicting it costs the least coverage."""
    closest = min(
        ((a, b) for i, a in enumerate(memories) for b in memories[i + 1 :]),
        key=lambda pair: redundancy(pair[0], pair[1].x, pair[1].y, pair[1].theta),
    )
    return min(closest, key=lambda m: m.stamp)


def _angle_diff(a: float, b: float) -> float:
    diff = abs(a - b) % (2 * math.pi)
    return min(diff, 2 * math.pi - diff)
