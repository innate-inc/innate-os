# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Likelihood-field lidar scan matching over the occupancy grid. Pure: no ROS, no I/O.

A pose's score is the mean, over lidar endpoints, of exp(-d² / 2σ²) with d the
endpoint's distance to the nearest occupied cell; its fit is the fraction of
endpoints within INLIER_M of one. Unknown space never counts as a hit, so a
pose whose beams pass through walls scores low. Searches are brute force over
(position, heading) sets — coarse with a wide σ, then refined with a narrow one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from brain_client.state.lidar import Lidar
    from brain_client.state.map import Map

INLIER_M = 0.10
FINE_SIGMA_M = 0.08
COARSE_SIGMA_M = 0.20
LASER_X_M = -0.0764  # base_link -> base_laser in mars.urdf, no rotation
OCCUPIED_MIN = 65  # nav2 map_server's occupied_thresh


@dataclass(frozen=True)
class Pose2D:
    x: float
    y: float
    theta: float
    fit: float = 0.0
    score: float = 0.0

    def distance(self, other: Pose2D) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def heading_gap(self, other: Pose2D) -> float:
        return abs(math.atan2(math.sin(self.theta - other.theta), math.cos(self.theta - other.theta)))


@dataclass(frozen=True)
class Scan:
    """Valid lidar endpoints as points in the base_link frame."""

    px: np.ndarray
    py: np.ndarray

    @staticmethod
    def from_lidar(lidar: Lidar, max_range: float = 11.5) -> Scan:
        ranges = np.asarray(lidar.ranges, dtype=np.float32)
        angles = lidar.angle_min + np.arange(len(ranges), dtype=np.float32) * lidar.angle_increment
        ok = np.isfinite(ranges) & (ranges > max(lidar.range_min, 0.05)) & (ranges < min(lidar.range_max, max_range))
        r, a = ranges[ok], angles[ok]
        return Scan((r * np.cos(a) + LASER_X_M).astype(np.float32), (r * np.sin(a)).astype(np.float32))

    def __len__(self) -> int:
        return len(self.px)

    def subsample(self, n: int) -> Scan:
        if len(self.px) <= n:
            return self
        idx = np.linspace(0, len(self.px) - 1, n).astype(int)
        return Scan(self.px[idx], self.py[idx])


@dataclass
class Grid:
    free: np.ndarray  # bool (h, w), row index grows with +y
    dist: np.ndarray  # metres to the nearest occupied cell
    resolution: float
    origin_x: float
    origin_y: float
    _fields: dict[float, np.ndarray] = field(default_factory=dict, repr=False)

    @staticmethod
    def from_map(nav_map: Map) -> Grid | None:
        cells = nav_map.grid
        if cells is None:
            return None
        occupied = cells >= OCCUPIED_MIN
        dist = cv2.distanceTransform((~occupied).astype(np.uint8), cv2.DIST_L2, 5).astype(np.float32)
        return Grid(cells == 0, dist * nav_map.resolution, nav_map.resolution, nav_map.origin_x, nav_map.origin_y)

    @property
    def shape(self) -> tuple[int, int]:
        h, w = self.free.shape
        return int(h), int(w)

    def cell(self, x: float, y: float) -> tuple[int, int] | None:
        row = math.floor((y - self.origin_y) / self.resolution)
        col = math.floor((x - self.origin_x) / self.resolution)
        h, w = self.shape
        return (row, col) if 0 <= row < h and 0 <= col < w else None

    def world(self, rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.origin_x + (cols + 0.5) * self.resolution, self.origin_y + (rows + 0.5) * self.resolution

    @cached_property
    def clearance(self) -> np.ndarray:
        """Metres from each cell to the nearest non-free cell."""
        return cv2.distanceTransform(self.free.astype(np.uint8), cv2.DIST_L2, 5) * self.resolution

    def likelihood(self, sigma: float) -> np.ndarray:
        """Flat uint8 likelihood image, padded by one zero cell so clipped endpoints score 0."""
        if sigma not in self._fields:
            lik = np.exp(-(self.dist**2) / (2 * sigma * sigma))
            self._fields[sigma] = np.pad((lik * 255).astype(np.uint8), 1).ravel()
        return self._fields[sigma]


def score_grid(grid: Grid, scan: Scan, xs: np.ndarray, ys: np.ndarray, thetas: np.ndarray, sigma: float) -> np.ndarray:
    """Likelihood score (N positions, T headings)."""
    lik = grid.likelihood(sigma)
    h, w = grid.shape
    inv = 1.0 / grid.resolution
    col0 = ((xs - grid.origin_x) * inv + 1.0).astype(np.float32)  # +1: the pad
    row0 = ((ys - grid.origin_y) * inv + 1.0).astype(np.float32)
    out = np.empty((len(xs), len(thetas)), dtype=np.float32)
    for j, theta in enumerate(thetas):
        cos, sin = np.float32(math.cos(theta) * inv), np.float32(math.sin(theta) * inv)
        cols = np.clip((col0[:, None] + (scan.px * cos - scan.py * sin)[None, :]).astype(np.int32), 0, w + 1)
        rows = np.clip((row0[:, None] + (scan.px * sin + scan.py * cos)[None, :]).astype(np.int32), 0, h + 1)
        out[:, j] = lik[rows * (w + 2) + cols].mean(axis=1) / 255.0
    return out


def evaluate(grid: Grid, scan: Scan, x: float, y: float, theta: float) -> Pose2D:
    cos, sin = math.cos(theta), math.sin(theta)
    ex, ey = x + scan.px * cos - scan.py * sin, y + scan.px * sin + scan.py * cos
    rows = np.floor((ey - grid.origin_y) / grid.resolution).astype(np.int32)
    cols = np.floor((ex - grid.origin_x) / grid.resolution).astype(np.int32)
    h, w = grid.shape
    inside = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
    d = np.full(rows.shape, 1.0, dtype=np.float32)
    d[inside] = grid.dist[rows[inside], cols[inside]]
    score = float(np.exp(-(d * d) / (2 * FINE_SIGMA_M**2)).mean()) if len(d) else 0.0
    fit = float((d < INLIER_M).mean()) if len(d) else 0.0
    return Pose2D(x, y, math.atan2(sin, cos), fit, score)


def refine(
    grid: Grid,
    scan: Scan,
    pose: Pose2D,
    span_m: float = 0.15,
    step_m: float = 0.025,
    span_deg: float = 6.0,
    step_deg: float = 1.0,
) -> Pose2D:
    best = pose
    for span, step, span_t, step_t in (
        (span_m, step_m, span_deg, step_deg),
        (step_m, step_m / 3, step_deg, step_deg / 3),
    ):
        offsets = np.arange(-span, span + 1e-9, step)
        gx, gy = np.meshgrid(best.x + offsets, best.y + offsets)
        xs, ys = gx.ravel(), gy.ravel()
        thetas = best.theta + np.radians(np.arange(-span_t, span_t + 1e-9, step_t))
        scores = score_grid(grid, scan, xs, ys, thetas, FINE_SIGMA_M)
        i, j = np.unravel_index(int(np.argmax(scores)), scores.shape)
        best = Pose2D(float(xs[i]), float(ys[i]), float(thetas[j]))
    return evaluate(grid, scan, best.x, best.y, best.theta)


def global_search(grid: Grid, scan: Scan, k: int = 8) -> list[Pose2D]:
    """The k best separated poses over the whole map."""
    return _coarse_to_fine(
        grid, scan, grid.free & (grid.clearance >= 0.12), spacing_m=0.2, heading_deg=6.0, k=k, sep_m=0.8, beams=90
    )


def region_search(grid: Grid, scan: Scan, region: np.ndarray, k: int = 4) -> list[Pose2D]:
    """The k best separated poses inside a boolean cell mask."""
    return _coarse_to_fine(
        grid,
        scan,
        grid.free & (grid.clearance >= 0.12) & region,
        spacing_m=0.1,
        heading_deg=5.0,
        k=k,
        sep_m=0.6,
        beams=120,
    )


def _coarse_to_fine(
    grid: Grid,
    scan: Scan,
    allowed: np.ndarray,
    *,
    spacing_m: float,
    heading_deg: float,
    k: int,
    sep_m: float,
    beams: int,
) -> list[Pose2D]:
    step = max(1, round(spacing_m / grid.resolution))
    rows, cols = np.nonzero(allowed)
    keep = (rows % step == 0) & (cols % step == 0)
    if not keep.any() or len(scan) == 0:
        return []
    xs, ys = grid.world(rows[keep], cols[keep])
    thetas = np.radians(np.arange(0.0, 360.0, heading_deg))
    scores = score_grid(grid, scan.subsample(beams), xs, ys, thetas, COARSE_SIGMA_M)
    seeds = _separated_peaks(xs, ys, thetas, scores, k, sep_m)
    refined = [
        refine(grid, scan, seed, span_m=spacing_m, step_m=spacing_m / 5, span_deg=heading_deg, step_deg=2.0)
        for seed in seeds
    ]
    return sorted(refined, key=lambda p: -p.score)


def _separated_peaks(
    xs: np.ndarray, ys: np.ndarray, thetas: np.ndarray, scores: np.ndarray, k: int, sep_m: float
) -> list[Pose2D]:
    best_heading = scores.argmax(axis=1)
    best = scores[np.arange(len(xs)), best_heading]
    picked: list[Pose2D] = []
    for i in np.argsort(-best):
        if len(picked) >= k:
            break
        if all(math.hypot(xs[i] - p.x, ys[i] - p.y) >= sep_m for p in picked):
            picked.append(Pose2D(float(xs[i]), float(ys[i]), float(thetas[best_heading[i]]), score=float(best[i])))
    return picked


def view_region(
    grid: Grid,
    x: float,
    y: float,
    theta: float,
    *,
    half_fov_deg: float = 45.0,
    reach_m: float = 10.0,
    walk_m: float = 3.5,
    pad_m: float = 0.5,
) -> np.ndarray:
    """Cells the robot could stand on to see the place a camera at (x, y, theta)
    saw: everything within walk_m of the capture point through free space (the
    same room from another angle), plus the floor the frame shows — its view
    wedge raycast to the first wall — padded by pad_m."""
    h, w = grid.shape
    origin = grid.cell(x, y)
    if origin is None:
        return np.zeros((h, w), bool)
    r0, c0 = origin
    n = int(walk_m / grid.resolution)
    # the walk can't leave this window, so grow it only inside it
    ra, rb, ca, cb = max(0, r0 - n - 1), min(h, r0 + n + 2), max(0, c0 - n - 1), min(w, c0 + n + 2)
    walk = np.zeros((rb - ra, cb - ca), bool)
    walk[r0 - ra, c0 - ca] = True
    region = np.zeros((h, w), bool)
    region[ra:rb, ca:cb] = _grow(walk, grid.free[ra:rb, ca:cb], n)
    region[r0, c0] = True
    wedge = np.zeros((h, w), np.uint8)
    corners = [(c0, r0)] + [
        _ray_end(grid, x, y, theta + math.radians(a), reach_m)
        for a in np.arange(-half_fov_deg, half_fov_deg + 1e-9, 1.5)
    ]
    cv2.fillPoly(wedge, [np.array(corners, np.int32)], (1,))
    return region | _grow(wedge.astype(bool) & grid.free, grid.free, int(pad_m / grid.resolution))


def _grow(seed: np.ndarray, free: np.ndarray, steps: int) -> np.ndarray:
    """``seed`` grown one cell per step (8-connected) without leaving ``free``."""
    kernel = np.ones((3, 3), np.uint8)
    grown = seed.astype(np.uint8)
    allowed = free.astype(np.uint8)
    for _ in range(steps):
        grown = np.minimum(np.asarray(cv2.dilate(grown, kernel)), allowed)
    return grown.astype(bool)


def _ray_end(grid: Grid, x: float, y: float, angle: float, reach_m: float) -> tuple[int, int]:
    step = grid.resolution / 2
    d = 0.0
    while d < reach_m:
        cell = grid.cell(x + (d + step) * math.cos(angle), y + (d + step) * math.sin(angle))
        if cell is None or not grid.free[cell]:
            break
        d += step
    return (
        int((x + d * math.cos(angle) - grid.origin_x) / grid.resolution),
        int((y + d * math.sin(angle) - grid.origin_y) / grid.resolution),
    )
