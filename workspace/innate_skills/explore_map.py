# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Global visual-coverage routing over a safe known occupancy map."""

import numpy as np
from innate_skills.find_next_person import (
    MIN_NEW_CELLS,
    FindNextPerson,
    _angular_distance,
    _choose_view,
    _covered_cells,
    _grid_travel_distances,
    _navigation_exclusions,
    _PlanningGrid,
    _too_close_to_unreachable,
    _View,
    _visible_cells,
)

from innate import Pose, SkillOutput, SkillReturn

MAX_ROUTE_VIEWPOINTS = 64

_STATUS_NAMES = {
    "SEARCH_RESET": "EXPLORATION_RESET",
    "SEARCH_ALREADY_INITIALIZED": "EXPLORATION_ALREADY_INITIALIZED",
    "SEARCH_OBSERVATION": "EXPLORATION_OBSERVATION",
    "SEARCH_UNREACHABLE": "EXPLORATION_UNREACHABLE",
    "SEARCH_INFRASTRUCTURE_FAILURE": "EXPLORATION_INFRASTRUCTURE_FAILURE",
    "SEARCH_VISUALIZATION": "EXPLORATION_VISUALIZATION",
    "SEARCH_EXHAUSTED": "EXPLORATION_COMPLETE",
}


def _route_distance_fields(plan: _PlanningGrid, pose: Pose, views: list[_View]) -> list[np.ndarray]:
    return [
        _grid_travel_distances(plan, pose),
        *[_grid_travel_distances(plan, Pose(view.x, view.y, view.theta)) for view in views],
    ]


def _ordered_route(plan: _PlanningGrid, pose: Pose, views: list[_View]) -> list[_View]:
    """Nearest-neighbor open route improved with obstacle-aware 2-opt."""
    if len(views) < 2:
        return list(views)
    fields = _route_distance_fields(plan, pose, views)

    def distance(source: int | None, destination: int) -> float:
        field = fields[0 if source is None else source + 1]
        view = views[destination]
        return float(field[view.row, view.col])

    remaining = set(range(len(views)))
    order: list[int] = []
    source: int | None = None
    heading = pose.theta
    while remaining:
        destination = min(
            remaining,
            key=lambda index: (
                distance(source, index),
                _angular_distance(heading, views[index].theta),
                -views[index].gain,
                index,
            ),
        )
        order.append(destination)
        remaining.remove(destination)
        source = destination
        heading = views[destination].theta

    def route_cost(indices: list[int]) -> float:
        if not indices:
            return 0.0
        return distance(None, indices[0]) + sum(
            distance(left, right) for left, right in zip(indices, indices[1:], strict=False)
        )

    improved = True
    while improved:
        improved = False
        baseline = route_cost(order)
        for start in range(len(order) - 1):
            for end in range(start + 1, len(order)):
                candidate = [*order[:start], *reversed(order[start : end + 1]), *order[end + 1 :]]
                candidate_cost = route_cost(candidate)
                if candidate_cost + 1e-6 < baseline:
                    order = candidate
                    baseline = candidate_cost
                    improved = True
    return [views[index] for index in order]


def _serialize_view(view: _View) -> dict:
    return {
        "row": view.row,
        "col": view.col,
        "x": round(view.x, 4),
        "y": round(view.y, 4),
        "theta": round(view.theta, 5),
    }


def _restore_view(plan: _PlanningGrid, raw: dict, covered: set[int]) -> _View | None:
    try:
        row, col = int(raw["row"]), int(raw["col"])
        x, y, theta = float(raw["x"]), float(raw["y"]), float(raw["theta"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= row < plan.height and 0 <= col < plan.width and plan.navigable[row, col]):
        return None
    visible = _visible_cells(plan, row, col, theta)
    gain = len(visible.difference(covered))
    if gain < MIN_NEW_CELLS:
        return None
    return _View(row, col, x, y, theta, visible, gain, float(gain))


def _plan_route(
    plan: _PlanningGrid,
    pose: Pose,
    covered: set[int],
    exclusions: list[dict],
    cancellation_check=None,
) -> list[_View]:
    """Plan full attainable coverage, remove redundant stops, then shorten it."""
    sequence: list[_View] = []
    simulated_observations: list[dict] = []
    simulated_pose = pose
    while len(sequence) < MAX_ROUTE_VIEWPOINTS:
        if cancellation_check is not None:
            cancellation_check()
        view, _ = _choose_view(
            plan,
            simulated_pose,
            simulated_observations,
            exclusions,
            cancellation_check=cancellation_check,
        )
        if view is None:
            break
        sequence.append(view)
        simulated_observations.append(
            {
                "x": view.x,
                "y": view.y,
                "theta": view.theta,
                "target_x": view.x,
                "target_y": view.y,
                "stopped_short": False,
            }
        )
        simulated_pose = Pose(view.x, view.y, view.theta)

    # The exhaustive sequence establishes what this map/camera model can
    # actually observe. Preserve that whole attainable set while deleting
    # stops whose view is already supplied by the others.
    target_coverage = set(covered)
    for view in sequence:
        target_coverage.update(view.visible)
    for index in range(len(sequence) - 1, -1, -1):
        without = set(covered)
        for other_index, view in enumerate(sequence):
            if other_index != index:
                without.update(view.visible)
        if target_coverage.issubset(without):
            sequence.pop(index)
    return _ordered_route(plan, pose, sequence)


class ExploreMap(FindNextPerson):
    """Move through safe known-free viewpoints until camera coverage is complete.

    Unknown, occupied, and unreachable cells are excluded. The planner
    first establishes the full attainable visibility set, deletes every
    redundant stop without losing any of it, then applies obstacle-aware open
    route optimization before motion begins. Each successful call advances one
    route segment and returns a fresh camera image. ``reset`` starts new
    coverage; ``visualize`` only renders it.
    """

    def _select_view(
        self,
        plan: _PlanningGrid,
        pose: Pose,
        state: dict,
    ) -> tuple[_View | None, set[int]]:
        covered = _covered_cells(plan, state["observations"])
        exclusions = _navigation_exclusions(state)
        route: list[_View] = []
        for raw in state.get("planned_route", []):
            view = _restore_view(plan, raw, covered)
            if view is None or _too_close_to_unreachable(view.x, view.y, exclusions):
                continue
            route.append(view)
        if not route:
            route = _plan_route(plan, pose, covered, exclusions, self.check_cancelled)
        state["planned_route"] = [_serialize_view(view) for view in route]
        self.storage["state"] = state
        return (route[0] if route else None), covered

    def _complete_selected_view(self, state: dict, view: _View) -> None:
        route = state.get("planned_route", [])
        if route:
            route.pop(0)

    def execute(self, reset: bool = False, visualize: bool = False) -> SkillReturn:
        output = super().execute(reset=reset, visualize=visualize)
        if not isinstance(output, SkillOutput):
            return output
        code, separator, remainder = output.message.partition(" ")
        translated = _STATUS_NAMES.get(code, code)
        return SkillOutput(
            f"{translated}{separator}{remainder}",
            data=output.data,
            status=output.status,
            image=output.image,
        )
