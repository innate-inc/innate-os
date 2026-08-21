# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Deterministic, ROS-free frontier planning on a live occupancy grid."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np

Cell = tuple[int, int]  # row, column


class FrontierPlanningError(ValueError):
    """The current grid/pose cannot safely produce exploration goals."""


@dataclass(frozen=True)
class GridGeometry:
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float = 0.0

    def __post_init__(self):
        if not math.isfinite(self.resolution) or self.resolution <= 0.0:
            raise FrontierPlanningError("map resolution must be positive")
        if not all(math.isfinite(v) for v in (self.origin_x, self.origin_y, self.origin_yaw)):
            raise FrontierPlanningError("map origin must be finite")

    def cell_to_world(self, row: int, col: int) -> tuple[float, float]:
        """Return the map-frame center of ``(row, col)``."""
        local_x = (col + 0.5) * self.resolution
        local_y = (row + 0.5) * self.resolution
        c = math.cos(self.origin_yaw)
        s = math.sin(self.origin_yaw)
        return (
            self.origin_x + c * local_x - s * local_y,
            self.origin_y + s * local_x + c * local_y,
        )

    def world_to_cell(self, x: float, y: float) -> Cell:
        """Return the occupancy cell containing a map-frame point."""
        dx = x - self.origin_x
        dy = y - self.origin_y
        c = math.cos(self.origin_yaw)
        s = math.sin(self.origin_yaw)
        local_x = c * dx + s * dy
        local_y = -s * dx + c * dy
        return (math.floor(local_y / self.resolution), math.floor(local_x / self.resolution))


@dataclass(frozen=True)
class FrontierGoal:
    row: int
    col: int
    x: float
    y: float
    yaw: float
    frontier_cells: int
    route_distance_m: float
    score: float
    cluster_anchor: Cell


_CARDINAL = ((-1, 0), (0, -1), (0, 1), (1, 0))
_EIGHT_CONNECTED = tuple((dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if not (dr == 0 and dc == 0))


def known_area_m2(grid: np.ndarray, resolution: float) -> float:
    return float(np.count_nonzero(np.asarray(grid) >= 0)) * resolution * resolution


def _adjacent_to(mask: np.ndarray, neighbors: Iterable[Cell] = _EIGHT_CONNECTED) -> np.ndarray:
    height, width = mask.shape
    padded = np.pad(mask.astype(bool, copy=False), 1, mode="constant", constant_values=False)
    adjacent = np.zeros((height, width), dtype=bool)
    for dr, dc in neighbors:
        adjacent |= padded[1 + dr : 1 + dr + height, 1 + dc : 1 + dc + width]
    return adjacent


def _inflate_obstacles(occupied: np.ndarray, clearance_m: float, resolution: float) -> np.ndarray:
    """Inflate occupied cells and the grid edge by a Euclidean radius."""
    if clearance_m <= 0.0:
        return occupied.astype(bool, copy=True)
    radius = int(math.ceil(clearance_m / resolution))
    height, width = occupied.shape
    padded = np.pad(occupied.astype(bool, copy=False), radius, mode="constant", constant_values=True)
    inflated = np.zeros((height, width), dtype=bool)
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            if math.hypot(dr, dc) * resolution > clearance_m + 1e-9:
                continue
            inflated |= padded[
                radius + dr : radius + dr + height,
                radius + dc : radius + dc + width,
            ]
    return inflated


def _nearest_free_cell(free: np.ndarray, start: Cell, max_snap_cells: int) -> Cell:
    height, width = free.shape
    row, col = start
    if 0 <= row < height and 0 <= col < width and free[row, col]:
        return start
    cells = np.argwhere(free)
    if cells.size == 0:
        raise FrontierPlanningError("map has no known-free cells")
    distances_sq = (cells[:, 0] - row) ** 2 + (cells[:, 1] - col) ** 2
    best_index = int(np.argmin(distances_sq))
    if int(distances_sq[best_index]) > max_snap_cells * max_snap_cells:
        raise FrontierPlanningError("map pose is not near known-free space")
    return (int(cells[best_index, 0]), int(cells[best_index, 1]))


def _nearest_reachable_cell(
    target: np.ndarray,
    traversable: np.ndarray,
    start: Cell,
    max_steps: int,
    check_cancelled: Callable[[], None] | None,
) -> tuple[Cell, int]:
    """Find the nearest target through a bounded four-connected traversal.

    This is intentionally not a Euclidean nearest-cell lookup: the latter can
    move the route seed through an occupied wall when the robot starts inside
    the route-clearance halo around an obstacle.
    """
    if target[start]:
        return start, 0

    height, width = traversable.shape
    distances = np.full((height, width), -1, dtype=np.int32)
    distances[start] = 0
    queue = deque([start])
    visited = 0
    while queue:
        row, col = queue.popleft()
        distance = int(distances[row, col])
        if distance >= max_steps:
            continue
        for dr, dc in _CARDINAL:
            nr, nc = row + dr, col + dc
            if not (0 <= nr < height and 0 <= nc < width):
                continue
            if not traversable[nr, nc] or distances[nr, nc] >= 0:
                continue
            next_distance = distance + 1
            if target[nr, nc]:
                return (nr, nc), next_distance
            distances[nr, nc] = next_distance
            queue.append((nr, nc))
        visited += 1
        if check_cancelled is not None and visited % 4096 == 0:
            check_cancelled()

    raise FrontierPlanningError("map pose is not near footprint-clear free space")


def _route_distances(
    free: np.ndarray,
    start: Cell,
    check_cancelled: Callable[[], None] | None,
) -> np.ndarray:
    """Four-connected BFS distance, preventing diagonal corner cutting."""
    height, width = free.shape
    distances = np.full((height, width), -1, dtype=np.int32)
    distances[start] = 0
    queue = deque([start])
    visited = 0
    while queue:
        row, col = queue.popleft()
        next_distance = int(distances[row, col]) + 1
        for dr, dc in _CARDINAL:
            nr, nc = row + dr, col + dc
            if 0 <= nr < height and 0 <= nc < width and free[nr, nc] and distances[nr, nc] < 0:
                distances[nr, nc] = next_distance
                queue.append((nr, nc))
        visited += 1
        if check_cancelled is not None and visited % 4096 == 0:
            check_cancelled()
    return distances


def _clusters(mask: np.ndarray, check_cancelled: Callable[[], None] | None) -> list[list[Cell]]:
    height, width = mask.shape
    unseen = mask.copy()
    clusters: list[list[Cell]] = []
    for seed_row, seed_col in np.argwhere(mask):
        seed = (int(seed_row), int(seed_col))
        if not unseen[seed]:
            continue
        unseen[seed] = False
        queue = deque([seed])
        cluster: list[Cell] = []
        while queue:
            row, col = queue.popleft()
            cluster.append((row, col))
            for dr, dc in _EIGHT_CONNECTED:
                nr, nc = row + dr, col + dc
                if 0 <= nr < height and 0 <= nc < width and unseen[nr, nc]:
                    unseen[nr, nc] = False
                    queue.append((nr, nc))
            if check_cancelled is not None and len(cluster) % 2048 == 0:
                check_cancelled()
        cluster.sort()
        clusters.append(cluster)
    return clusters


def _is_excluded(x: float, y: float, excluded: Iterable[tuple[float, float]], radius_m: float) -> bool:
    radius_sq = radius_m * radius_m
    return any((x - ex) ** 2 + (y - ey) ** 2 <= radius_sq for ex, ey in excluded)


def find_frontier_goals(
    grid: np.ndarray,
    geometry: GridGeometry,
    robot_x: float,
    robot_y: float,
    *,
    occupied_threshold: int = 50,
    route_clearance_m: float = 0.17,
    obstacle_clearance_m: float = 0.30,
    min_frontier_length_m: float = 0.35,
    min_goal_distance_m: float = 0.25,
    excluded_world: Iterable[tuple[float, float]] = (),
    exclusion_radius_m: float = 0.60,
    information_gain_weight: float = 1.0,
    travel_cost_weight: float = 0.25,
    check_cancelled: Callable[[], None] | None = None,
) -> list[FrontierGoal]:
    """Return ranked, reachable known-free cells beside unknown space.

    The frontier itself is represented by *free* cells adjacent to unknown,
    never by the unknown centroid. This matches Nav2's ``allow_unknown=false``
    contract. Reachability uses a footprint-sized clearance that rejects
    definitely impassable corridors while retaining valid narrow doorways.
    Destination safety separately uses the larger obstacle clearance.
    """

    grid = np.asarray(grid)
    if grid.ndim != 2 or grid.shape[0] == 0 or grid.shape[1] == 0:
        raise FrontierPlanningError("occupancy grid must be a non-empty 2D array")
    if not all(math.isfinite(v) for v in (robot_x, robot_y)):
        raise FrontierPlanningError("map pose must be finite")
    if (
        route_clearance_m < 0.0
        or obstacle_clearance_m < 0.0
        or min_frontier_length_m <= 0.0
        or exclusion_radius_m < 0.0
    ):
        raise FrontierPlanningError("frontier distances must be non-negative")

    free = (grid >= 0) & (grid < occupied_threshold)
    occupied = grid >= occupied_threshold
    start = geometry.world_to_cell(robot_x, robot_y)
    max_snap_cells = max(1, int(math.ceil(0.5 / geometry.resolution)))
    start = _nearest_free_cell(free, start, max_snap_cells)
    route_unsafe = _inflate_obstacles(occupied, route_clearance_m, geometry.resolution)
    route_free = free & ~route_unsafe
    max_route_seed_steps = int(math.ceil(route_clearance_m / geometry.resolution))
    route_start, seed_distance = _nearest_reachable_cell(
        route_free,
        free,
        start,
        max_route_seed_steps,
        check_cancelled,
    )
    distances = _route_distances(route_free, route_start, check_cancelled)
    distances[distances >= 0] += seed_distance
    reachable = distances >= 0

    frontier_mask = free & reachable & _adjacent_to(grid < 0)
    if not np.any(frontier_mask):
        return []
    unsafe = _inflate_obstacles(occupied, obstacle_clearance_m, geometry.resolution)
    safe_destination = frontier_mask & ~unsafe
    excluded = tuple(excluded_world)

    goals: list[FrontierGoal] = []
    for cluster in _clusters(frontier_mask, check_cancelled):
        if len(cluster) * geometry.resolution + 1e-9 < min_frontier_length_m:
            continue
        cluster_anchor = cluster[0]
        centroid_row = sum(cell[0] for cell in cluster) / len(cluster)
        centroid_col = sum(cell[1] for cell in cluster) / len(cluster)

        candidates = []
        for row, col in cluster:
            if not safe_destination[row, col]:
                continue
            route_distance_m = float(distances[row, col]) * geometry.resolution
            if route_distance_m + 1e-9 < min_goal_distance_m:
                continue
            x, y = geometry.cell_to_world(row, col)
            if _is_excluded(x, y, excluded, exclusion_radius_m):
                continue
            centroid_distance_sq = (row - centroid_row) ** 2 + (col - centroid_col) ** 2
            candidates.append((centroid_distance_sq, route_distance_m, row, col, x, y))
        if not candidates:
            continue
        _, route_distance_m, row, col, x, y = min(candidates)

        unknown_neighbors: set[Cell] = set()
        height, width = grid.shape
        for frontier_row, frontier_col in cluster:
            for dr, dc in _EIGHT_CONNECTED:
                nr, nc = frontier_row + dr, frontier_col + dc
                if 0 <= nr < height and 0 <= nc < width and grid[nr, nc] < 0:
                    unknown_neighbors.add((nr, nc))
        if not unknown_neighbors:
            continue
        # Average the exact map-frame cell centers. Flooring the mean grid
        # coordinate biases the heading by as much as half a cell on each
        # axis, especially for asymmetric frontier boundaries.
        unknown_world = [geometry.cell_to_world(r, c) for r, c in unknown_neighbors]
        unknown_x = sum(point[0] for point in unknown_world) / len(unknown_world)
        unknown_y = sum(point[1] for point in unknown_world) / len(unknown_world)
        yaw = math.atan2(unknown_y - y, unknown_x - x)
        frontier_length_m = len(cluster) * geometry.resolution
        score = information_gain_weight * frontier_length_m - travel_cost_weight * route_distance_m
        goals.append(
            FrontierGoal(
                row=row,
                col=col,
                x=x,
                y=y,
                yaw=yaw,
                frontier_cells=len(cluster),
                route_distance_m=route_distance_m,
                score=score,
                cluster_anchor=cluster_anchor,
            )
        )
        if check_cancelled is not None:
            check_cancelled()

    goals.sort(
        key=lambda goal: (
            -goal.score,
            goal.route_distance_m,
            goal.cluster_anchor[0],
            goal.cluster_anchor[1],
            goal.row,
            goal.col,
        )
    )
    return goals
