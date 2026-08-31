"""A* over the sim's own occupancy grid, so an oracle can navigate a real
floorplan instead of following hand-placed waypoints.

Why this exists: the first oracle drove straight lines. That is fine in a nine
metre hall and useless in an apartment, and it made the validity gate unable to
tell "this challenge is broken" from "my follower is too dumb to get there".
Planning removes the second explanation.

The grid comes from VirtualMars.occupancy_grid: values -1 unknown / 0 free /
100 occupied, row-major from (origin_x, origin_y). Unknown is treated as
BLOCKED for planning -- it is outside the mapped floor, and a path through it
is a path through a wall the rasteriser could not see under.
"""

from __future__ import annotations

import heapq
import math
import os

import numpy as np

# Half the base diagonal (0.188 x 0.182) is 0.131 m; this adds ~3 cm of margin.
# Cells within this of an obstacle are unusable, which is what stops a plan
# from shaving a doorjamb.
#
# Overridable because it is the difference between "this goal is unreachable"
# and "my planner is too timid", and a benchmark that cannot tell those apart
# is not worth running. See check_points.py.
# 0.145: the true half-diagonal plus ~1.4 cm. 0.16 looked like a harmless
# safety margin and was not -- it sealed a doorway in the apartment and made
# five separate goal clusters unreachable, which the gate faithfully reported
# as ~37 broken challenges. They were fine; the planner would not fit through
# its own inflation. Measured with check_points.py at 0.16 / 0.145 / 0.131.
ROBOT_RADIUS_M = float(os.environ.get("BENCH_ROBOT_RADIUS", "0.145"))


class NavMap:
    def __init__(self, grid: np.ndarray, origin_x: float, origin_y: float, resolution: float):
        self.res = resolution
        self.ox, self.oy = origin_x, origin_y
        self.h, self.w = grid.shape
        blocked = grid != 0
        self.blocked = _inflate(blocked, max(1, int(round(ROBOT_RADIUS_M / resolution))))

    @classmethod
    def from_sim(cls, mars, resolution: float = 0.06) -> NavMap:
        grid, ox, oy = mars.occupancy_grid(resolution)
        _stamp_primitives(mars, grid, ox, oy, resolution)
        return cls(grid, ox, oy, resolution)

    def to_cell(self, x: float, y: float) -> tuple[int, int]:
        return (int((y - self.oy) / self.res), int((x - self.ox) / self.res))

    def to_world(self, r: int, c: int) -> tuple[float, float]:
        return (self.ox + (c + 0.5) * self.res, self.oy + (r + 0.5) * self.res)

    def in_bounds(self, r: int, c: int) -> bool:
        return 0 <= r < self.h and 0 <= c < self.w

    def free(self, r: int, c: int) -> bool:
        return self.in_bounds(r, c) and not self.blocked[r, c]

    def nearest_free(self, x: float, y: float, max_radius_m: float = 1.2) -> tuple[int, int] | None:
        """The closest usable cell to a world point. Goals sit on furniture and
        inside props all the time; the reachable thing is the space beside them."""
        r0, c0 = self.to_cell(x, y)
        if self.free(r0, c0):
            return (r0, c0)
        for rad in range(1, int(max_radius_m / self.res) + 1):
            best = None
            for dr in range(-rad, rad + 1):
                for dc in (-rad, rad) if abs(dr) != rad else range(-rad, rad + 1):
                    r, c = r0 + dr, c0 + dc
                    if self.free(r, c):
                        d = dr * dr + dc * dc
                        if best is None or d < best[0]:
                            best = (d, (r, c))
            if best:
                return best[1]
        return None

    def plan(self, start: tuple[float, float], goal: tuple[float, float]) -> list[tuple[float, float]] | None:
        """World-space waypoints from start to goal, or None if unreachable.

        None is the answer the gate actually needs: it separates a challenge
        whose goal cannot be reached at all from one the follower merely
        fumbled."""
        s = self.nearest_free(*start)
        g = self.nearest_free(*goal)
        if s is None or g is None:
            return None
        if s == g:
            return [self.to_world(*g)]

        # 8-connected A*, octile heuristic.
        nbrs = [
            (-1, 0, 1.0),
            (1, 0, 1.0),
            (0, -1, 1.0),
            (0, 1, 1.0),
            (-1, -1, 1.4142),
            (-1, 1, 1.4142),
            (1, -1, 1.4142),
            (1, 1, 1.4142),
        ]

        def h(a, b):
            dr, dc = abs(a[0] - b[0]), abs(a[1] - b[1])
            return (dr + dc) + (1.4142 - 2) * min(dr, dc)

        open_q = [(h(s, g), 0.0, s)]
        came: dict = {s: None}
        cost = {s: 0.0}
        while open_q:
            _f, gc, cur = heapq.heappop(open_q)
            if cur == g:
                break
            if gc > cost.get(cur, math.inf):
                continue
            for dr, dc, step in nbrs:
                nxt = (cur[0] + dr, cur[1] + dc)
                if not self.free(*nxt):
                    continue
                # No corner-cutting: a diagonal is only legal when both
                # orthogonal neighbours are free, or the path clips a corner
                # the base cannot actually pass.
                if dr and dc and not (self.free(cur[0] + dr, cur[1]) and self.free(cur[0], cur[1] + dc)):
                    continue
                ng = gc + step
                if ng < cost.get(nxt, math.inf):
                    cost[nxt] = ng
                    came[nxt] = cur
                    heapq.heappush(open_q, (ng + h(nxt, g), ng, nxt))
        if g not in came:
            return None

        cells = []
        cur = g
        while cur is not None:
            cells.append(cur)
            cur = came[cur]
        cells.reverse()
        return [self.to_world(*c) for c in _simplify(cells, self)]


# The z-slab occupancy_grid treats as wall: above rugs and thresholds, below
# ceilings. Matches the constants inside core.occupancy_grid.
# The height band a geom must intersect to count as an obstacle.
#
# SLAB_LO is 5 mm, not the 10 cm it started at. add_planar_base gives the robot
# x, y and yaw and no z, so it cannot climb ANYTHING; a 12 mm station pad is a
# wall to it. At 0.10 the planner skipped exactly such a pad in the household
# map, planned through it, and the robot drove into the edge and held 18 N
# against it for the rest of the episode while the grid insisted the cell was
# free.
#
# It cannot be 0: the floor is itself a collidable geom whose top is at z = 0
# by convention in these maps, and stamping it would block the world. 5 mm
# clears the floor and catches everything standing on it.
SLAB_LO = float(os.environ.get("BENCH_SLAB_LO", "0.005"))
SLAB_HI = 1.4


def _stamp_primitives(mars, grid: np.ndarray, ox: float, oy: float, res: float) -> None:
    """Mark static PRIMITIVE geoms as occupied, in place.

    core._rasterize_static_slab walks collision TRIANGLES, so a room authored
    as boxes and cylinders (statics.py) contributes nothing to it: the grid
    came back with zero blocked cells and A* planned straight through walls.
    The robot then drove into one and sat there until the time limit, which
    reads exactly like a broken challenge.

    Footprints are stamped as the geom's xy AABB. Exact for the axis-aligned
    boxes that make up walls and floors; conservative for a rotated one, which
    over-blocks a little around angled furniture and never under-blocks.

    The proper home for this is core._rasterize_static_slab -- occupancy_grid
    is meant to rasterise the collision world, and primitives are part of it.
    Kept here so the fix stays inside the benchmark.
    """
    import mujoco

    h, w = grid.shape
    for gid in range(mars.model.ngeom):
        body = mars.model.body(int(mars.model.geom_bodyid[gid]))
        name = body.name or ""
        # Authored rooms only: props are dynamic obstacles, not map features,
        # and the robot is not an obstacle to itself.
        if not name.startswith("room_"):
            continue
        gtype = int(mars.model.geom_type[gid])
        if gtype not in (
            mujoco.mjtGeom.mjGEOM_BOX,
            mujoco.mjtGeom.mjGEOM_CYLINDER,
            mujoco.mjtGeom.mjGEOM_SPHERE,
            mujoco.mjtGeom.mjGEOM_CAPSULE,
        ):
            continue
        if mars.model.geom_contype[gid] == 0 and mars.model.geom_conaffinity[gid] == 0:
            continue  # decor: drawn, never collided with

        pos = mars.data.geom_xpos[gid]
        mat = mars.data.geom_xmat[gid].reshape(3, 3)
        size = mars.model.geom_size[gid]
        if gtype == mujoco.mjtGeom.mjGEOM_BOX:
            half = size[:3]
        elif gtype in (mujoco.mjtGeom.mjGEOM_CYLINDER, mujoco.mjtGeom.mjGEOM_CAPSULE):
            half = np.array([size[0], size[0], size[1]])
        else:
            half = np.array([size[0], size[0], size[0]])

        # World-axis half-extents of the rotated box: |R| @ half.
        ext = np.abs(mat) @ half
        if pos[2] + ext[2] < SLAB_LO or pos[2] - ext[2] > SLAB_HI:
            continue  # floors and low trim are not obstacles

        c0 = int((pos[0] - ext[0] - ox) / res)
        c1 = int((pos[0] + ext[0] - ox) / res)
        r0 = int((pos[1] - ext[1] - oy) / res)
        r1 = int((pos[1] + ext[1] - oy) / res)
        grid[max(0, r0) : min(h, r1 + 1), max(0, c0) : min(w, c1 + 1)] = 100


def _inflate(blocked: np.ndarray, cells: int) -> np.ndarray:
    """Grow obstacles by `cells` using repeated 4-neighbour dilation. Plain
    numpy so the bench has no scipy dependency."""
    out = blocked.copy()
    for _ in range(cells):
        nxt = out.copy()
        nxt[1:, :] |= out[:-1, :]
        nxt[:-1, :] |= out[1:, :]
        nxt[:, 1:] |= out[:, :-1]
        nxt[:, :-1] |= out[:, 1:]
        out = nxt
    return out


def _simplify(cells: list[tuple[int, int]], nav: NavMap) -> list[tuple[int, int]]:
    """Drop intermediate cells a straight line already covers. A* returns one
    waypoint per grid cell -- hundreds for a corridor -- and a follower that
    re-aims at each of them crawls."""
    if len(cells) < 3:
        return cells
    out = [cells[0]]
    anchor = 0
    for i in range(2, len(cells)):
        if not _clear_line(cells[anchor], cells[i], nav):
            out.append(cells[i - 1])
            anchor = i - 1
    out.append(cells[-1])
    return out


def _clear_line(a: tuple[int, int], b: tuple[int, int], nav: NavMap) -> bool:
    """Supercover line check between two cells."""
    r0, c0 = a
    r1, c1 = b
    n = max(abs(r1 - r0), abs(c1 - c0))
    if n == 0:
        return True
    for i in range(n + 1):
        r = round(r0 + (r1 - r0) * i / n)
        c = round(c0 + (c1 - c0) * i / n)
        if not nav.free(r, c):
            return False
    return True
