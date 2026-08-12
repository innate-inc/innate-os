#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Reading Nav2's local costmap, so the robot can refuse to clip what the
policy plans through.

The policy is trained on a 0.1 m-radius agent and predicts a floor path; it has
no notion of the robot's actual footprint, so its paths shave corners the real
body cannot. Rather than build obstacle sensing, this reads the local costmap
innate-os already publishes.

That costmap is currently LIDAR-ONLY -- the SpatioTemporalVoxelLayer over the
depth cloud is disabled -- so it sees nothing above or below the 0.17 m scan
plane. Table edges, sofa arms and low sills are invisible here, and the blocked
detector (pursuit.BlockedDetector, which watches odometry disagree with the
command) is what catches those.

Cost convention is Nav2's OccupancyGrid mapping (verified against a live
window): 0 free, 1-98 the inflation gradient, 99 inscribed, 100 lethal, -1
unknown. `99` already means "the robot's centre cannot be here" — the inflation
layer computed it from the live footprint — so the hard test needs no radius of
our own. The 1-98 band is a distance-to-obstacle field, which is exactly what a
repulsion term wants.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LETHAL = 100
INSCRIBED = 99          # robot centre here collides; hard veto
UNKNOWN = -1

# Unknown is treated as drivable: the window is only 6 m across, so its edges
# and any sensor shadow read as unknown, and refusing those would freeze the
# robot in the open. Lethal/inscribed is the honest signal.
_UNKNOWN_COST = 0


@dataclass(frozen=True)
class Costmap:
    """An odom-frame occupancy window. Row-major, origin at the grid's corner."""

    grid: np.ndarray          # (height, width) int16
    resolution: float
    origin_x: float
    origin_y: float

    @classmethod
    def from_msg(cls, msg: dict) -> Costmap:
        """Build from a nav_msgs/OccupancyGrid (as rosbridge JSON or a ROS msg
        exposing the same fields)."""
        info = msg["info"] if isinstance(msg, dict) else msg.info
        data = msg["data"] if isinstance(msg, dict) else msg.data
        pos = info["origin"]["position"] if isinstance(msg, dict) else info.origin.position
        w = int(info["width"] if isinstance(msg, dict) else info.width)
        h = int(info["height"] if isinstance(msg, dict) else info.height)
        res = float(info["resolution"] if isinstance(msg, dict) else info.resolution)
        ox = float(pos["x"] if isinstance(msg, dict) else pos.x)
        oy = float(pos["y"] if isinstance(msg, dict) else pos.y)
        return cls(np.asarray(data, dtype=np.int16).reshape(h, w), res, ox, oy)

    # ---- sampling ---------------------------------------------------------
    def cost_at(self, x: float, y: float) -> int:
        """Cost at an odom point; UNKNOWN outside the window."""
        col = int((x - self.origin_x) / self.resolution)
        row = int((y - self.origin_y) / self.resolution)
        if not (0 <= row < self.grid.shape[0] and 0 <= col < self.grid.shape[1]):
            return UNKNOWN
        return int(self.grid[row, col])

    def costs_at(self, pts: np.ndarray) -> np.ndarray:
        """Vectorised cost_at over an (N, 2) array of odom points."""
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        cols = ((pts[:, 0] - self.origin_x) / self.resolution).astype(int)
        rows = ((pts[:, 1] - self.origin_y) / self.resolution).astype(int)
        h, w = self.grid.shape
        inside = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
        out = np.full(len(pts), UNKNOWN, dtype=np.int16)
        out[inside] = self.grid[rows[inside], cols[inside]]
        return out

    def blocked(self, x: float, y: float) -> bool:
        return self.cost_at(x, y) >= INSCRIBED

    # ---- path queries -----------------------------------------------------
    def safe_length(self, path_xy: np.ndarray) -> float:
        """Arc length of the path prefix the robot's centre may occupy, in
        metres. Samples between waypoints at half a cell — the policy's points
        sit 0.25 m apart, five cells, so testing only the vertices would step
        straight over a corner."""
        pts = np.asarray(path_xy, dtype=np.float64).reshape(-1, 2)
        if len(pts) < 2:
            return 0.0
        if self.blocked(*pts[0]):
            return 0.0          # already inside something; nowhere safe to go
        step = self.resolution / 2.0
        travelled = 0.0
        for a, b in zip(pts[:-1], pts[1:], strict=True):
            seg = float(np.hypot(*(b - a)))
            n = max(1, int(np.ceil(seg / step)))
            ts = np.linspace(0.0, 1.0, n + 1)[1:]
            samples = a + np.outer(ts, b - a)
            costs = self.costs_at(samples)
            hit = np.flatnonzero(costs >= INSCRIBED)
            if len(hit):
                return travelled + seg * float(ts[hit[0]])
            travelled += seg
        return travelled

    def worst_cost(self, path_xy: np.ndarray) -> int:
        """Highest inflation cost along the path — how close it runs to
        something, for speed scaling."""
        pts = np.asarray(path_xy, dtype=np.float64).reshape(-1, 2)
        if not len(pts):
            return 0
        costs = self.costs_at(pts)
        costs = np.where(costs == UNKNOWN, _UNKNOWN_COST, costs)
        return int(costs.max())

    def repulsion(self, x: float, y: float, reach_m: float = 0.4) -> np.ndarray:
        """Unit vector pointing away from nearby obstacles, from the inflation
        gradient; zero where the neighbourhood is uniformly free.

        This is what widens corners. The policy's line grazes a convex corner
        because it plans for a 0.1 m robot; the inflation field around that
        corner rises toward it, so descending the field nudges the pursuit
        target outward and the robot takes the wider line instead of stopping.
        """
        d = max(self.resolution, reach_m / 2.0)
        cx = self._sample(x + d, y) - self._sample(x - d, y)
        cy = self._sample(x, y + d) - self._sample(x, y - d)
        grad = np.array([cx, cy], dtype=np.float64)   # points uphill, toward cost
        norm = float(np.hypot(*grad))
        if norm < 1e-9:
            return np.zeros(2)
        return -grad / norm                            # downhill, away from cost

    def _sample(self, x: float, y: float) -> float:
        c = self.cost_at(x, y)
        return float(_UNKNOWN_COST if c == UNKNOWN else c)
