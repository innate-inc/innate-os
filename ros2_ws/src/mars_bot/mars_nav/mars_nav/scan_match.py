# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Likelihood-field lidar scan matching over the occupancy grid. Pure: no ROS, no I/O.

A pose's score is the mean, over lidar endpoints, of exp(-d² / 2σ²) with d the
endpoint's distance to the nearest occupied cell; its fit is the fraction of
endpoints within INLIER_M of one. Unknown space never counts as a hit, so a
pose whose beams pass through walls scores low. Searches are brute force over
(position, heading) sets — coarse with a wide σ, then refined with a narrow one.
How sure a match is: the best pose's share of the evidence among every distinct
pose the search finds, each weighted by its likelihood score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from nav_msgs.msg import OccupancyGrid
    from sensor_msgs.msg import LaserScan

INLIER_M = 0.10
FINE_SIGMA_M = 0.08
COARSE_SIGMA_M = 0.20
LASER_X_M = -0.0764  # base_link -> base_laser in mars.urdf, no rotation
OCCUPIED_MIN = 65  # nav2 map_server's occupied_thresh
TAU = 0.02  # score gap worth a factor e when the best pose explains the whole scan...
TAU_SLOPE = 0.15  # ...widened per unit of what it leaves unexplained (clutter, a stale map)
MERGE_M = 0.4
MERGE_DEG = 20.0
MIN_FIT = 0.55  # a confident match puts at least this share of endpoints on a wall


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
    def from_laser_scan(msg: LaserScan, max_range: float) -> Scan:
        ranges = np.asarray(msg.ranges, dtype=np.float32)
        angles = msg.angle_min + np.arange(len(ranges), dtype=np.float32) * msg.angle_increment
        ok = np.isfinite(ranges) & (ranges > max(msg.range_min, 0.05)) & (ranges < min(msg.range_max, max_range))
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
    def from_occupancy_grid(msg: OccupancyGrid) -> Grid:
        info = msg.info
        cells = np.asarray(msg.data, dtype=np.int8).reshape((info.height, info.width))
        occupied = cells >= OCCUPIED_MIN
        dist = cv2.distanceTransform((~occupied).astype(np.uint8), cv2.DIST_L2, 5).astype(np.float32)
        return Grid(cells == 0, dist * info.resolution, info.resolution, info.origin.position.x, info.origin.position.y)

    @property
    def shape(self) -> tuple[int, int]:
        h, w = self.free.shape
        return int(h), int(w)

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


@dataclass(frozen=True)
class Estimate:
    pose: Pose2D  # the best pose, fitted to the scan
    share: float  # its share of the evidence among every distinct candidate

    def confident(self, min_share: float) -> bool:
        return self.share >= min_share and self.pose.fit >= MIN_FIT


def locate(grid: Grid, scan: Scan) -> Estimate | None:
    """The best pose over the whole map and how much of the evidence it holds, or None when nothing fits."""
    poses = _distinct(global_search(grid, scan))
    if not poses:
        return None
    best = poses[0]
    tau = TAU + TAU_SLOPE * (1.0 - best.score)
    return Estimate(best, 1.0 / sum(math.exp((p.score - best.score) / tau) for p in poses))


def _distinct(poses: list[Pose2D]) -> list[Pose2D]:
    """Best first, dropping any pose within MERGE_M and MERGE_DEG of a better one."""
    kept: list[Pose2D] = []
    for pose in sorted(poses, key=lambda p: -p.score):
        if all(pose.distance(k) >= MERGE_M or pose.heading_gap(k) >= math.radians(MERGE_DEG) for k in kept):
            kept.append(pose)
    return kept


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
    """The k best distinct poses over the whole map, apart in position or in heading."""
    return _coarse_to_fine(
        grid,
        scan,
        grid.free & (grid.clearance >= 0.12),
        spacing_m=0.2,
        heading_deg=6.0,
        k=k,
        sep_m=0.8,
        sep_deg=30.0,
        beams=90,
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
    sep_deg: float,
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
    seeds = _separated_peaks(xs, ys, thetas, scores, k, sep_m, sep_deg)
    refined = [
        refine(grid, scan, seed, span_m=spacing_m, step_m=spacing_m / 5, span_deg=heading_deg, step_deg=2.0)
        for seed in seeds
    ]
    return sorted(refined, key=lambda p: -p.score)


def _separated_peaks(
    xs: np.ndarray, ys: np.ndarray, thetas: np.ndarray, scores: np.ndarray, k: int, sep_m: float, sep_deg: float
) -> list[Pose2D]:
    """The k best (position, heading) cells, each sep_m or sep_deg from every better pick:
    one spot facing two ways is two hypotheses (a corridor fits its own reverse)."""
    picked: list[Pose2D] = []
    for flat in np.argsort(-scores, axis=None):
        if len(picked) >= k:
            break
        i, j = divmod(int(flat), len(thetas))
        pose = Pose2D(float(xs[i]), float(ys[i]), float(thetas[j]), score=float(scores[i, j]))
        if all(pose.distance(p) >= sep_m or pose.heading_gap(p) >= math.radians(sep_deg) for p in picked):
            picked.append(pose)
    return picked
