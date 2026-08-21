# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Focused unit tests for the ROS-free autonomous-mapping planner."""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPO_ROOT / "workspace"))

from innate_skills._frontier_planner import (  # noqa: E402
    GridGeometry,
    _nearest_reachable_cell,
    find_frontier_goals,
    known_area_m2,
)


def _world(geometry: GridGeometry, row: int, col: int) -> tuple[float, float]:
    return geometry.cell_to_world(row, col)


def _single_frontier_grid() -> np.ndarray:
    grid = np.full((7, 8), 100, dtype=np.int16)
    grid[3, 1] = 0
    grid[3, 2:5] = 49
    grid[3, 5] = -1
    return grid


def _rooms_joined_by_corridor(corridor_width_cells: int) -> np.ndarray:
    """Return two rooms joined by a centered 5 cm/cell corridor."""
    grid = np.full((40, 80), 100, dtype=np.int16)
    grid[5:35, 5:26] = 0
    grid[5:35, 45:71] = 0
    first_row = 20 - corridor_width_cells // 2
    grid[first_row : first_row + corridor_width_cells, 25:46] = 0
    grid[10:30, 65:70] = -1
    return grid


def test_grid_geometry_round_trips_cell_centers_with_rotated_origin():
    geometry = GridGeometry(resolution=0.5, origin_x=10.0, origin_y=-2.0, origin_yaw=math.pi / 2)

    x, y = geometry.cell_to_world(1, 2)

    assert x == pytest.approx(9.25)
    assert y == pytest.approx(-0.75)
    assert geometry.world_to_cell(x, y) == (1, 2)


def test_known_free_values_below_threshold_are_traversable_and_threshold_is_occupied():
    geometry = GridGeometry(resolution=1.0, origin_x=0.0, origin_y=0.0)
    grid = _single_frontier_grid()
    robot_x, robot_y = _world(geometry, 3, 1)

    goals = find_frontier_goals(
        grid,
        geometry,
        robot_x,
        robot_y,
        occupied_threshold=50,
        obstacle_clearance_m=0.0,
        min_frontier_length_m=0.1,
    )

    assert [(goal.row, goal.col) for goal in goals] == [(3, 4)]
    assert grid[goals[0].row, goals[0].col] == 49

    blocked = grid.copy()
    blocked[3, 3] = 50
    assert (
        find_frontier_goals(
            blocked,
            geometry,
            robot_x,
            robot_y,
            occupied_threshold=50,
            obstacle_clearance_m=0.0,
            min_frontier_length_m=0.1,
        )
        == []
    )
    assert known_area_m2(np.array([[-1, 0, 49, 50, 100]]), resolution=0.5) == pytest.approx(1.0)


def test_unreachable_frontier_island_is_not_returned():
    geometry = GridGeometry(resolution=1.0, origin_x=0.0, origin_y=0.0)
    grid = np.full((9, 15), 100, dtype=np.int16)
    grid[2:7, 1:6] = 0
    grid[2:7, 6] = -1
    grid[2:7, 9:14] = 0
    grid[2:7, 8] = -1
    robot_x, robot_y = _world(geometry, 4, 2)

    goals = find_frontier_goals(
        grid,
        geometry,
        robot_x,
        robot_y,
        obstacle_clearance_m=0.0,
        min_frontier_length_m=1.0,
    )

    assert len(goals) == 1
    assert goals[0].col == 5
    assert grid[goals[0].row, goals[0].col] == 0


def test_route_clearance_rejects_frontier_behind_30_cm_bottleneck():
    geometry = GridGeometry(resolution=0.05, origin_x=0.0, origin_y=0.0)
    grid = _rooms_joined_by_corridor(corridor_width_cells=6)
    robot_x, robot_y = _world(geometry, 20, 10)

    assert find_frontier_goals(
        grid,
        geometry,
        robot_x,
        robot_y,
        route_clearance_m=0.0,
        min_frontier_length_m=0.1,
    )
    goals = find_frontier_goals(grid, geometry, robot_x, robot_y, min_frontier_length_m=0.1)

    assert goals == []


def test_route_clearance_retains_frontier_behind_35_cm_doorway():
    geometry = GridGeometry(resolution=0.05, origin_x=0.0, origin_y=0.0)
    grid = _rooms_joined_by_corridor(corridor_width_cells=7)
    robot_x, robot_y = _world(geometry, 20, 10)

    goals = find_frontier_goals(grid, geometry, robot_x, robot_y, min_frontier_length_m=0.1)

    assert goals
    assert goals[0].col >= 64


def test_route_seed_search_does_not_snap_across_occupied_wall():
    traversable = np.ones((7, 7), dtype=bool)
    traversable[:, 3] = False
    target = np.zeros_like(traversable)
    target[3, 4] = True  # Euclidean-nearest target, across the wall.
    target[0, 0] = True  # Farther target, reachable on the robot's side.

    route_start, distance = _nearest_reachable_cell(
        target,
        traversable,
        start=(3, 2),
        max_steps=10,
        check_cancelled=None,
    )

    assert route_start == (0, 0)
    assert distance == 5


def test_goal_clearance_avoids_nearby_obstacle_with_deterministic_tie_break():
    geometry = GridGeometry(resolution=1.0, origin_x=0.0, origin_y=0.0)
    grid = np.full((15, 15), 100, dtype=np.int16)
    grid[3:12, 2:10] = 0
    grid[3:12, 10] = -1
    grid[7, 8] = 100
    robot_x, robot_y = _world(geometry, 7, 3)
    kwargs = {
        "obstacle_clearance_m": 1.1,
        "min_frontier_length_m": 1.0,
    }

    first = find_frontier_goals(grid, geometry, robot_x, robot_y, **kwargs)
    second = find_frontier_goals(grid, geometry, robot_x, robot_y, **kwargs)

    assert first == second
    assert [(goal.row, goal.col) for goal in first] == [(6, 9)]
    assert math.hypot(first[0].row - 7, first[0].col - 8) > 1.1


def test_goal_heading_uses_exact_unknown_cell_centroid():
    geometry = GridGeometry(resolution=1.0, origin_x=2.0, origin_y=-3.0, origin_yaw=0.35)
    grid = np.full((9, 9), 100, dtype=np.int16)
    grid[4, 4] = 0
    unknown_cells = [(3, 4), (4, 5)]
    for cell in unknown_cells:
        grid[cell] = -1
    robot_x, robot_y = _world(geometry, 4, 4)

    goals = find_frontier_goals(
        grid,
        geometry,
        robot_x,
        robot_y,
        obstacle_clearance_m=0.0,
        min_frontier_length_m=0.1,
        min_goal_distance_m=0.0,
    )

    assert len(goals) == 1
    unknown_world = [_world(geometry, row, col) for row, col in unknown_cells]
    centroid_x = sum(x for x, _ in unknown_world) / len(unknown_world)
    centroid_y = sum(y for _, y in unknown_world) / len(unknown_world)
    expected_yaw = math.atan2(centroid_y - goals[0].y, centroid_x - goals[0].x)
    assert goals[0].yaw == pytest.approx(expected_yaw)
    assert goals[0].yaw != pytest.approx(geometry.origin_yaw - math.pi / 2)


def test_world_exclusion_survives_grid_origin_expansion():
    grid = _single_frontier_grid()
    geometry = GridGeometry(resolution=1.0, origin_x=0.0, origin_y=0.0)
    robot_x, robot_y = _world(geometry, 3, 1)
    kwargs = {
        "obstacle_clearance_m": 0.0,
        "min_frontier_length_m": 0.1,
    }
    original = find_frontier_goals(grid, geometry, robot_x, robot_y, **kwargs)
    assert len(original) == 1

    expanded = np.full((grid.shape[0], grid.shape[1] + 1), 100, dtype=grid.dtype)
    expanded[:, 1:] = grid
    shifted_geometry = GridGeometry(resolution=1.0, origin_x=-1.0, origin_y=0.0)
    shifted = find_frontier_goals(expanded, shifted_geometry, robot_x, robot_y, **kwargs)

    assert len(shifted) == 1
    assert shifted[0].x == pytest.approx(original[0].x)
    assert shifted[0].y == pytest.approx(original[0].y)
    assert (
        find_frontier_goals(
            expanded,
            shifted_geometry,
            robot_x,
            robot_y,
            excluded_world=[(original[0].x, original[0].y)],
            exclusion_radius_m=0.1,
            **kwargs,
        )
        == []
    )


def test_fully_known_reachable_map_has_no_frontiers():
    geometry = GridGeometry(resolution=0.1, origin_x=0.0, origin_y=0.0)
    grid = np.zeros((20, 20), dtype=np.int8)
    grid[[0, -1], :] = 100
    grid[:, [0, -1]] = 100
    robot_x, robot_y = _world(geometry, 10, 10)

    assert find_frontier_goals(grid, geometry, robot_x, robot_y) == []


def test_cancellation_checkpoint_runs_before_goals_are_returned():
    class CancelProbe(BaseException):
        pass

    geometry = GridGeometry(resolution=1.0, origin_x=0.0, origin_y=0.0)
    grid = _single_frontier_grid()
    robot_x, robot_y = _world(geometry, 3, 1)

    def cancel():
        raise CancelProbe

    with pytest.raises(CancelProbe):
        find_frontier_goals(
            grid,
            geometry,
            robot_x,
            robot_y,
            obstacle_clearance_m=0.0,
            min_frontier_length_m=0.1,
            check_cancelled=cancel,
        )
