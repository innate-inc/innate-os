# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Choose fresh camera viewpoints until the reachable map has been inspected."""

import hashlib
import io
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from heapq import heappop, heappush
from pathlib import Path

import numpy as np
from innate_skills.mission_run import active_run_id, artifact_root, write_artifact_set
from innate_skills.navigate_to_position import (
    NavigateToPosition,
    NavigationExecutionError,
    NavigationInfrastructureError,
    NavigationPathIndeterminateError,
)

from innate import Head, HeadState, MainImage, Map, Pose, Skill, SkillCancelled, SkillFailed, SkillOutput, SkillReturn

# Planning on the source map (normally 5 cm/cell) would evaluate far more
# camera rays than this mission needs. Pool conservatively to a small visual
# coverage grid while Nav2 continues to plan against the full-resolution map.
TARGET_GRID_RESOLUTION_M = 0.25
VIEWPOINT_SPACING_M = 0.65
UNREACHABLE_SUPPRESSION_RADIUS_M = VIEWPOINT_SPACING_M
NAVIGATION_FAILURE_CONFIRMATIONS = 2
NAVIGATION_FAILURE_MATCH_TOLERANCE_M = 0.01
NAVIGATION_FAILURE_RETRY_DELAY_S = 45.0
RECOVERY_MIN_DISTANCE_M = 0.35
RECOVERY_MAX_DISTANCE_M = 4.0
ROBOT_CLEARANCE_M = 0.25
# Slightly inside the main camera's roughly 84-degree horizontal FOV so a
# cell is never credited merely because it sat just outside the real image.
CAMERA_FOV_RAD = math.radians(80.0)
CAMERA_RANGE_M = 4.0
CAMERA_RAYS = 41
HEADING_COUNT = 8
MAX_VIEWPOINTS = 240
MIN_NEW_CELLS = 6
INITIAL_TRAVEL_COST_CELLS_PER_M = 12.0
SWEEP_TRAVEL_COST_CELLS_PER_M = 1.0
NOVELTY_BONUS_CELLS_PER_M = 10.0
HANDLED_PERSON_ESTIMATED_DISTANCE_M = 1.5
HANDLED_PERSON_VIEW_PENALTY_CELLS = 220.0
HANDLED_PERSON_PROXIMITY_RADIUS_M = 2.0
HANDLED_PERSON_PROXIMITY_PENALTY_CELLS_PER_M = 80.0
FRAME_TIMEOUT_S = 5.0
PERSON_VIEW_HEAD_PITCH_DEG = 20.0
HEAD_POSITION_TOLERANCE_DEG = 2.0
HEAD_SETTLE_TIMEOUT_S = 4.0
ARRIVAL_POSITION_TOLERANCE_M = 0.5
ARRIVAL_HEADING_TOLERANCE_RAD = math.radians(35.0)
# Nav2 already owns collision safety. Stop only inside the same tolerance used
# to accept an observation, so ray-cast gain is scored from approximately the
# pose the camera will actually reach instead of more than a metre beyond it.
VIEWPOINT_STANDOFF_M = ARRIVAL_POSITION_TOLERANCE_M
VIEWPOINT_STANDOFF_TOLERANCE_M = 0.25
STATE_VERSION = 1
ARTIFACT_RELATIVE_DIR = Path("workspace/skill_storage/household_orders")


def _result(code: str, payload: dict | None = None, *, image: bytes | None = None) -> SkillOutput:
    """Return the small status contract exposed to the agent."""
    data = payload or {}
    message = f"{code} {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}"
    return SkillOutput(message, data=data, image=image)


@dataclass(frozen=True)
class _PlanningGrid:
    resolution: float
    origin_x: float
    origin_y: float
    origin_theta: float
    traversable: np.ndarray
    blocked: np.ndarray
    safe: np.ndarray
    reachable: np.ndarray
    navigable: np.ndarray

    @property
    def height(self) -> int:
        return int(self.traversable.shape[0])

    @property
    def width(self) -> int:
        return int(self.traversable.shape[1])


@dataclass(frozen=True)
class _View:
    row: int
    col: int
    x: float
    y: float
    theta: float
    visible: frozenset[int]
    gain: int
    score: float


def _wrap_angle(theta: float) -> float:
    return math.atan2(math.sin(theta), math.cos(theta))


def _angular_distance(a: float, b: float) -> float:
    return abs(_wrap_angle(a - b))


def _map_fingerprint(map_state: Map, cells: np.ndarray) -> str:
    metadata = (
        f"{map_state.width}:{map_state.height}:{map_state.resolution:.9f}:"
        f"{map_state.origin_x:.6f}:{map_state.origin_y:.6f}:{map_state.origin_theta:.9f}:"
    ).encode()
    return hashlib.sha256(metadata + np.ascontiguousarray(cells).tobytes()).hexdigest()[:20]


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Conservative square dilation; outside the map counts as blocked."""
    if radius <= 0:
        return mask.copy()
    height, width = mask.shape
    padded = np.pad(mask, radius, mode="constant", constant_values=True)
    result = np.zeros_like(mask, dtype=bool)
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            result |= padded[radius + dr : radius + dr + height, radius + dc : radius + dc + width]
    return result


def _coarsen(cells: np.ndarray, source_resolution: float) -> tuple[np.ndarray, np.ndarray, float]:
    factor = max(1, int(math.ceil(TARGET_GRID_RESOLUTION_M / source_resolution)))
    height, width = cells.shape
    coarse_h = int(math.ceil(height / factor))
    coarse_w = int(math.ceil(width / factor))
    padded = np.full((coarse_h * factor, coarse_w * factor), -1, dtype=np.int16)
    padded[:height, :width] = cells
    blocks = padded.reshape(coarse_h, factor, coarse_w, factor).transpose(0, 2, 1, 3)

    known_free = (blocks >= 0) & (blocks < 50)
    # Every source cell must be known-free. This keeps thin walls and unknown
    # stripes from disappearing inside a pooled planning cell, matching Nav2's
    # allow_unknown=false contract.
    blocked = ~known_free.all(axis=(2, 3))
    traversable = ~blocked
    return traversable, blocked, source_resolution * factor


def _world_to_cell(plan: _PlanningGrid, x: float, y: float) -> tuple[int, int]:
    dx = x - plan.origin_x
    dy = y - plan.origin_y
    cos_o = math.cos(plan.origin_theta)
    sin_o = math.sin(plan.origin_theta)
    local_x = cos_o * dx + sin_o * dy
    local_y = -sin_o * dx + cos_o * dy
    return int(math.floor(local_y / plan.resolution)), int(math.floor(local_x / plan.resolution))


def _cell_to_world(plan: _PlanningGrid, row: int, col: int) -> tuple[float, float]:
    local_x = (col + 0.5) * plan.resolution
    local_y = (row + 0.5) * plan.resolution
    cos_o = math.cos(plan.origin_theta)
    sin_o = math.sin(plan.origin_theta)
    return (
        plan.origin_x + cos_o * local_x - sin_o * local_y,
        plan.origin_y + sin_o * local_x + cos_o * local_y,
    )


def _nearest_cell(mask: np.ndarray, row: int, col: int) -> tuple[int, int] | None:
    points = np.argwhere(mask)
    if not len(points):
        return None
    distances = (points[:, 0] - row) ** 2 + (points[:, 1] - col) ** 2
    best = points[int(np.argmin(distances))]
    return int(best[0]), int(best[1])


def _connected_component(mask: np.ndarray, seed: tuple[int, int]) -> np.ndarray:
    reachable = np.zeros_like(mask, dtype=bool)
    row, col = seed
    if not (0 <= row < mask.shape[0] and 0 <= col < mask.shape[1] and mask[row, col]):
        return reachable
    queue = deque([(row, col)])
    reachable[row, col] = True
    while queue:
        row, col = queue.popleft()
        for nr, nc in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
            if 0 <= nr < mask.shape[0] and 0 <= nc < mask.shape[1] and mask[nr, nc] and not reachable[nr, nc]:
                reachable[nr, nc] = True
                queue.append((nr, nc))
    return reachable


def _build_planning_grid(map_state: Map, pose: Pose) -> _PlanningGrid | None:
    cells = map_state.grid
    if cells is None or cells.ndim != 2 or not cells.size or map_state.resolution <= 0:
        return None
    traversable, blocked, resolution = _coarsen(cells, map_state.resolution)
    clearance_cells = max(1, int(math.ceil(ROBOT_CLEARANCE_M / resolution)))
    safe = traversable & ~_dilate(blocked, clearance_cells)
    skeleton = _PlanningGrid(
        resolution=resolution,
        origin_x=map_state.origin_x,
        origin_y=map_state.origin_y,
        origin_theta=map_state.origin_theta,
        traversable=traversable,
        blocked=blocked,
        safe=safe,
        reachable=np.zeros_like(traversable),
        navigable=np.zeros_like(traversable),
    )
    pose_cell = _world_to_cell(skeleton, pose.x, pose.y)
    seed = _nearest_cell(traversable, *pose_cell)
    if seed is None:
        return None
    reachable = _connected_component(traversable, seed)
    # Clearance filters viewpoint destinations. It must not also become the
    # transit graph: coarse doorway erosion can split rooms that Nav2 can still
    # connect. Nav2 validates each route against the full-resolution costmap.
    navigable = safe & reachable
    return _PlanningGrid(
        resolution=resolution,
        origin_x=map_state.origin_x,
        origin_y=map_state.origin_y,
        origin_theta=map_state.origin_theta,
        traversable=traversable,
        blocked=blocked,
        safe=safe,
        reachable=reachable,
        navigable=navigable,
    )


def _sample_viewpoints(plan: _PlanningGrid) -> list[tuple[int, int]]:
    """Return a deterministic, map-anchored set of viewpoint destinations."""
    points = np.argwhere(plan.navigable)
    if not len(points):
        return []

    # Anchor the farthest-point lattice to the map rather than the robot pose.
    # The centroid seed avoids biasing the finite set toward a map corner.
    centroid = points.mean(axis=0)
    distance_to_centroid = (points[:, 0] - centroid[0]) ** 2 + (points[:, 1] - centroid[1]) ** 2
    first = points[int(np.argmin(distance_to_centroid))]
    chosen = [(int(first[0]), int(first[1]))]
    minimum_distance = (points[:, 0] - chosen[0][0]) ** 2 + (points[:, 1] - chosen[0][1]) ** 2
    spacing_cells = max(1, int(math.ceil(VIEWPOINT_SPACING_M / plan.resolution)))
    spacing_sq = spacing_cells * spacing_cells

    while len(chosen) < MAX_VIEWPOINTS:
        next_index = int(np.argmax(minimum_distance))
        if int(minimum_distance[next_index]) < spacing_sq:
            break
        row, col = (int(v) for v in points[next_index])
        chosen.append((row, col))
        candidate_distance = (points[:, 0] - row) ** 2 + (points[:, 1] - col) ** 2
        minimum_distance = np.minimum(minimum_distance, candidate_distance)
    return chosen


def _dense_viewpoint_lattice(
    plan: _PlanningGrid,
    sparse_viewpoints: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return deterministic plan-cell fallbacks not present in the sparse set."""
    sparse = set(sparse_viewpoints)
    candidates = [
        (int(row), int(col)) for row, col in np.argwhere(plan.navigable) if (int(row), int(col)) not in sparse
    ]
    if len(candidates) <= MAX_VIEWPOINTS:
        return candidates
    if MAX_VIEWPOINTS <= 0:
        return []
    if MAX_VIEWPOINTS == 1:
        return [candidates[len(candidates) // 2]]

    # Preserve deterministic row-major lattice ordering while spreading a
    # bounded recovery set across the whole map instead of one corner.
    last_index = len(candidates) - 1
    return [candidates[index * last_index // (MAX_VIEWPOINTS - 1)] for index in range(MAX_VIEWPOINTS)]


def _grid_travel_distances(plan: _PlanningGrid, pose: Pose) -> np.ndarray:
    """Approximate Nav2 route length from pose to every reachable grid cell."""
    distances = np.full((plan.height, plan.width), np.inf, dtype=float)
    pose_row, pose_col = _world_to_cell(plan, pose.x, pose.y)
    seed = _nearest_cell(plan.reachable, pose_row, pose_col)
    if seed is None:
        return distances

    distances[seed] = 0.0
    queue: list[tuple[float, int, int]] = [(0.0, seed[0], seed[1])]
    neighbors = (
        (-1, 0, 1.0),
        (1, 0, 1.0),
        (0, -1, 1.0),
        (0, 1, 1.0),
        (-1, -1, math.sqrt(2.0)),
        (-1, 1, math.sqrt(2.0)),
        (1, -1, math.sqrt(2.0)),
        (1, 1, math.sqrt(2.0)),
    )
    while queue:
        distance, row, col = heappop(queue)
        if distance > distances[row, col]:
            continue
        for dr, dc, step_cells in neighbors:
            next_row = row + dr
            next_col = col + dc
            if not (0 <= next_row < plan.height and 0 <= next_col < plan.width):
                continue
            if not plan.reachable[next_row, next_col]:
                continue
            # Do not let diagonal motion cut through a blocked corner.
            if dr and dc and (not plan.reachable[row + dr, col] or not plan.reachable[row, col + dc]):
                continue
            candidate = distance + step_cells * plan.resolution
            if candidate >= distances[next_row, next_col]:
                continue
            distances[next_row, next_col] = candidate
            heappush(queue, (candidate, next_row, next_col))
    return distances


def _visible_cells(plan: _PlanningGrid, row: int, col: int, theta: float) -> frozenset[int]:
    """Ray-cast the camera wedge on the coarsened grid."""
    visible: set[int] = set()
    relative_heading = theta - plan.origin_theta
    step_m = plan.resolution * 0.45
    step_count = max(1, int(math.ceil(CAMERA_RANGE_M / step_m)))
    for ray in range(CAMERA_RAYS):
        fraction = ray / (CAMERA_RAYS - 1)
        angle = relative_heading - CAMERA_FOV_RAD / 2.0 + CAMERA_FOV_RAD * fraction
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        previous = (-1, -1)
        for step in range(step_count + 1):
            distance = min(CAMERA_RANGE_M, step * step_m)
            rr = int(math.floor(row + 0.5 + sin_a * distance / plan.resolution))
            cc = int(math.floor(col + 0.5 + cos_a * distance / plan.resolution))
            if (rr, cc) == previous:
                continue
            previous = (rr, cc)
            if not (0 <= rr < plan.height and 0 <= cc < plan.width):
                break
            if plan.blocked[rr, cc]:
                break
            if plan.reachable[rr, cc] and plan.traversable[rr, cc]:
                visible.add(rr * plan.width + cc)
            if distance >= CAMERA_RANGE_M:
                break
    return frozenset(visible)


def _covered_cells(plan: _PlanningGrid, observations: list[dict]) -> set[int]:
    covered: set[int] = set()
    for observation in observations:
        try:
            row, col = _world_to_cell(plan, float(observation["x"]), float(observation["y"]))
            theta = float(observation["theta"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= row < plan.height and 0 <= col < plan.width:
            covered.update(_visible_cells(plan, row, col, theta))
    return covered


def _too_close_to_unreachable(x: float, y: float, unreachable: list[dict]) -> bool:
    for failed in unreachable:
        try:
            if math.hypot(x - float(failed["x"]), y - float(failed["y"])) <= UNREACHABLE_SUPPRESSION_RADIUS_M:
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def _distance_from_observations(x: float, y: float, observations: list[dict]) -> float:
    distances: list[float] = []
    for observation in observations:
        try:
            distances.append(math.hypot(x - float(observation["x"]), y - float(observation["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return min(distances) if distances else 0.0


def _handled_person_anchors() -> list[tuple[float, float]]:
    """Estimated map positions for residents whose orders are already durable."""
    root = artifact_root()
    try:
        roster = json.loads((root / "resident_roster.json").read_text())
        notes = json.loads((root / "mission_notes.json").read_text())
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []
    run_id = active_run_id()
    if not run_id or roster.get("run_id") != run_id or notes.get("run_id") != run_id:
        return []
    handled = notes.get("notes")
    residents = roster.get("residents")
    if not isinstance(handled, dict) or not isinstance(residents, list):
        return []
    anchors: list[tuple[float, float]] = []
    for resident in residents:
        if not isinstance(resident, dict) or resident.get("encounter_id") not in handled:
            continue
        pose = resident.get("last_seen_pose")
        try:
            x, y, theta = float(pose["x"]), float(pose["y"]), float(pose["theta"])
        except (KeyError, TypeError, ValueError):
            continue
        anchors.append(
            (
                x + HANDLED_PERSON_ESTIMATED_DISTANCE_M * math.cos(theta),
                y + HANDLED_PERSON_ESTIMATED_DISTANCE_M * math.sin(theta),
            )
        )
    return anchors


def _standoff_target_was_observed(x: float, y: float, observations: list[dict]) -> bool:
    """Whether this requested position already produced a stopped-short view."""
    for observation in observations:
        if not observation.get("stopped_short"):
            continue
        try:
            distance = math.hypot(x - float(observation["target_x"]), y - float(observation["target_y"]))
        except (KeyError, TypeError, ValueError):
            continue
        if distance <= NAVIGATION_FAILURE_MATCH_TOLERANCE_M:
            return True
    return False


def _choose_view(
    plan: _PlanningGrid,
    pose: Pose,
    observations: list[dict],
    unreachable: list[dict],
    handled_person_anchors: list[tuple[float, float]] | None = None,
    cancellation_check=None,
) -> tuple[_View | None, set[int]]:
    covered = _covered_cells(plan, observations)
    route_distances = _grid_travel_distances(plan, pose)
    travel_cost = SWEEP_TRAVEL_COST_CELLS_PER_M if observations else INITIAL_TRAVEL_COST_CELLS_PER_M
    sparse_viewpoints = _sample_viewpoints(plan)
    pose_row, pose_col = _world_to_cell(plan, pose.x, pose.y)
    person_anchors = handled_person_anchors or []

    def score_candidates(candidates: list[tuple[int, int]]) -> _View | None:
        best: _View | None = None
        for index, (row, col) in enumerate(candidates):
            if cancellation_check is not None and index % 12 == 0:
                cancellation_check()
            x, y = _cell_to_world(plan, row, col)
            if _too_close_to_unreachable(x, y, unreachable):
                continue
            # A standoff stop deliberately observes from a reachable pose before
            # the requested target. Suppress every heading at that target after
            # the observation so its never-reached predicted coverage cannot win
            # the score forever.
            if _standoff_target_was_observed(x, y, observations):
                continue
            distance = float(route_distances[row, col])
            if not math.isfinite(distance):
                continue
            nearby = bool(observations) and math.hypot(x - pose.x, y - pose.y) <= VIEWPOINT_STANDOFF_M
            # Nav2 deliberately stops this far before an exploration target.
            # When the target is already inside that radius, execution can only
            # rotate at the current pose. Score the view from that same pose so
            # dense fallback cells cannot promise unseen coverage from places
            # the robot will never move to.
            visible_row, visible_col = (pose_row, pose_col) if nearby else (row, col)
            evaluation_x, evaluation_y = (pose.x, pose.y) if nearby else (x, y)
            evaluation_distance = 0.0 if nearby else distance
            for heading_index in range(HEADING_COUNT):
                theta = _wrap_angle(plan.origin_theta + heading_index * math.tau / HEADING_COUNT)
                visible = _visible_cells(plan, visible_row, visible_col, theta)
                gain = len(visible.difference(covered))
                if gain < MIN_NEW_CELLS:
                    continue
                # The first observation stays local. Once the search has evidence,
                # spread stops across unseen wings instead of exhaustively sweeping
                # one room before visiting the rest of the home.
                novelty = _distance_from_observations(evaluation_x, evaluation_y, observations)
                person_view_penalty = 0.0
                person_proximity_penalty = 0.0
                for person_x, person_y in person_anchors:
                    person_row, person_col = _world_to_cell(plan, person_x, person_y)
                    if (
                        0 <= person_row < plan.height
                        and 0 <= person_col < plan.width
                        and person_row * plan.width + person_col in visible
                    ):
                        person_view_penalty += HANDLED_PERSON_VIEW_PENALTY_CELLS
                    proximity = math.hypot(evaluation_x - person_x, evaluation_y - person_y)
                    person_proximity_penalty += (
                        max(0.0, HANDLED_PERSON_PROXIMITY_RADIUS_M - proximity)
                        * HANDLED_PERSON_PROXIMITY_PENALTY_CELLS_PER_M
                    )
                score = (
                    gain
                    - travel_cost * evaluation_distance
                    + NOVELTY_BONUS_CELLS_PER_M * novelty
                    - 0.5 * _angular_distance(theta, pose.theta)
                    - person_view_penalty
                    - person_proximity_penalty
                )
                view = _View(row, col, x, y, theta, visible, gain, score)
                if best is None or view.score > best.score:
                    best = view
        return best

    best = score_candidates(sparse_viewpoints)
    if best is None:
        best = score_candidates(_dense_viewpoint_lattice(plan, sparse_viewpoints))
    return best, covered


def _coverage_fraction(plan: _PlanningGrid, covered: set[int]) -> float:
    total = int((plan.traversable & plan.reachable).sum())
    return min(1.0, len(covered) / total) if total else 0.0


def _rotation_at_reached_viewpoint(
    view: _View,
    pose: Pose,
    *,
    allow_visual_standoff: bool = False,
) -> float | None:
    """Return the turn needed when visual standoff makes translation a no-op."""
    tolerance = VIEWPOINT_STANDOFF_M if allow_visual_standoff else ARRIVAL_POSITION_TOLERANCE_M
    if math.hypot(view.x - pose.x, view.y - pose.y) <= tolerance:
        return round(math.degrees(_wrap_angle(view.theta - pose.theta)), 2)
    return None


def _standoff_heading(view: _View, pose: Pose) -> float:
    """Face the blocked visual target when stopping before its exact point."""
    dx = view.x - pose.x
    dy = view.y - pose.y
    if math.hypot(dx, dy) <= 1e-6:
        return view.theta
    return math.atan2(dy, dx)


def _same_failed_viewpoint(failure: dict, view: _View) -> bool:
    try:
        return (
            math.hypot(view.x - float(failure["x"]), view.y - float(failure["y"]))
            <= NAVIGATION_FAILURE_MATCH_TOLERANCE_M
        )
    except (KeyError, TypeError, ValueError):
        return False


def _record_navigation_failure(state: dict, view: _View) -> bool:
    """Return whether repeated failures now confirm this viewpoint unreachable."""
    record = {"x": round(view.x, 4), "y": round(view.y, 4), "theta": round(view.theta, 5)}
    for index, failure in enumerate(state["navigation_failures"]):
        if not _same_failed_viewpoint(failure, view):
            continue
        try:
            attempts = int(failure.get("attempts", 0)) + 1
        except (TypeError, ValueError):
            attempts = 1
        if attempts < NAVIGATION_FAILURE_CONFIRMATIONS:
            state["navigation_failures"][index] = {
                **record,
                "attempts": attempts,
                "retry_after": time.time() + NAVIGATION_FAILURE_RETRY_DELAY_S,
            }
            return False
        state["navigation_failures"].pop(index)
        if not _too_close_to_unreachable(view.x, view.y, state["unreachable"]):
            state["unreachable"].append(record)
        return True
    state["navigation_failures"].append(
        {**record, "attempts": 1, "retry_after": time.time() + NAVIGATION_FAILURE_RETRY_DELAY_S}
    )
    return False


def _defer_navigation_execution_failure(state: dict, view: _View) -> None:
    """Temporarily avoid a route without claiming its destination is impossible."""
    record = {
        "x": round(view.x, 4),
        "y": round(view.y, 4),
        "theta": round(view.theta, 5),
        "retry_after": time.time() + NAVIGATION_FAILURE_RETRY_DELAY_S,
    }
    for index, failure in enumerate(state["navigation_failures"]):
        if _same_failed_viewpoint(failure, view):
            attempts = int(failure.get("attempts", 0)) + 1
            state["navigation_failures"][index] = {**record, "attempts": attempts}
            return
    state["navigation_failures"].append({**record, "attempts": 1})


def _navigation_exclusions(state: dict) -> list[dict]:
    now = time.time()
    deferred = []
    for failure in state["navigation_failures"]:
        retry_after = failure.get("retry_after")
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and retry_after > now:
            deferred.append(failure)
    return [*state["unreachable"], *deferred]


def _clear_navigation_failure(state: dict, view: _View) -> bool:
    remaining = [failure for failure in state["navigation_failures"] if not _same_failed_viewpoint(failure, view)]
    changed = len(remaining) != len(state["navigation_failures"])
    state["navigation_failures"] = remaining
    return changed


def _pose_record(record: dict | None) -> tuple[float, float, float] | None:
    if not isinstance(record, dict):
        return None
    try:
        return float(record["x"]), float(record["y"]), float(record["theta"])
    except (KeyError, TypeError, ValueError):
        return None


def _same_recovery_position(record: dict, target: tuple[float, float, float]) -> bool:
    parsed = _pose_record(record)
    return parsed is not None and math.hypot(parsed[0] - target[0], parsed[1] - target[1]) < RECOVERY_MIN_DISTANCE_M


def _recovery_target(plan: _PlanningGrid, state: dict, pose: Pose) -> tuple[float, float, float] | None:
    """Return the nearest current-map-safe pose that the robot previously reached."""
    candidates = [state.get("recovery_anchor"), *reversed(state["observations"])]
    rejected = state.get("recovery_rejections", [])
    best: tuple[float, float, float] | None = None
    best_distance = math.inf
    considered: list[tuple[float, float, float]] = []
    for candidate in candidates:
        parsed = _pose_record(candidate)
        if parsed is None:
            continue
        x, y, theta = parsed
        if any(_same_recovery_position(record, parsed) for record in rejected):
            continue
        if any(math.hypot(previous[0] - x, previous[1] - y) < RECOVERY_MIN_DISTANCE_M for previous in considered):
            continue
        considered.append(parsed)
        distance = math.hypot(x - pose.x, y - pose.y)
        if not RECOVERY_MIN_DISTANCE_M <= distance <= RECOVERY_MAX_DISTANCE_M:
            continue
        row, col = _world_to_cell(plan, x, y)
        if not (0 <= row < plan.height and 0 <= col < plan.width and plan.safe[row, col]):
            continue
        if distance < best_distance:
            best = (x, y, theta)
            best_distance = distance
    return best


def _relative_pose_goal(pose: Pose, target: tuple[float, float, float]) -> tuple[float, float, float]:
    """Express a map-frame target relative to the robot's current pose."""
    target_x, target_y, target_theta = target
    dx = target_x - pose.x
    dy = target_y - pose.y
    cos_yaw = math.cos(pose.theta)
    sin_yaw = math.sin(pose.theta)
    return (
        cos_yaw * dx + sin_yaw * dy,
        -sin_yaw * dx + cos_yaw * dy,
        _wrap_angle(target_theta - pose.theta),
    )


def _draw_pose_marker(draw, plan: _PlanningGrid, pose: dict, cell_px: int, top: int, color, *, cross=False) -> None:
    try:
        x = float(pose["x"])
        y = float(pose["y"])
        theta = float(pose["theta"])
    except (KeyError, TypeError, ValueError):
        return
    row, col = _world_to_cell(plan, x, y)
    if not (0 <= row < plan.height and 0 <= col < plan.width):
        return
    center_x = col * cell_px + cell_px / 2
    center_y = top + (plan.height - 1 - row) * cell_px + cell_px / 2
    radius = max(3, cell_px * 0.65)
    if cross:
        draw.line((center_x - radius, center_y - radius, center_x + radius, center_y + radius), fill=color, width=3)
        draw.line((center_x - radius, center_y + radius, center_x + radius, center_y - radius), fill=color, width=3)
        return
    draw.ellipse((center_x - radius, center_y - radius, center_x + radius, center_y + radius), fill=color)
    tip_x = x + math.cos(theta) * plan.resolution * 2.2
    tip_y = y + math.sin(theta) * plan.resolution * 2.2
    tip_row, tip_col = _world_to_cell(plan, tip_x, tip_y)
    draw.line(
        (
            center_x,
            center_y,
            tip_col * cell_px + cell_px / 2,
            top + (plan.height - 1 - tip_row) * cell_px + cell_px / 2,
        ),
        fill=color,
        width=3,
    )


def _coverage_artifact(
    plan: _PlanningGrid,
    state: dict,
    covered: set[int],
    pose: Pose,
    *,
    status: str,
    target: _View | None = None,
) -> bytes | None:
    """Refresh planner-estimated search coverage and return a JPEG preview."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    cell_px = max(5, min(10, 760 // max(plan.width, plan.height, 1)))
    top = 190
    map_width = plan.width * cell_px
    map_height = plan.height * cell_px
    width = max(760, map_width)
    height = top + map_height + 58
    canvas = Image.new("RGB", (width, height), (17, 20, 26))
    draw = ImageDraw.Draw(canvas)

    coverage = _coverage_fraction(plan, covered)
    reachable = plan.traversable & plan.reachable
    reachable_count = int(reachable.sum())
    draw.text((16, 14), "HOUSEHOLD EXPLORATION COVERAGE", fill=(245, 247, 250))
    draw.text(
        (16, 38),
        f"{coverage:.0%} planner-estimated coverage · {len(state['observations'])} observation poses · "
        f"{len(state['unreachable'])} unreachable targets · {status}",
        fill=(166, 177, 194),
    )
    legend_rows = [
        [
            ((20, 23, 29), "outside / blocked / unknown"),
            ((38, 44, 54), "mapped floor outside viewpoint clearance"),
        ],
        [
            ((47, 55, 67), "safe viewpoint floor, not covered"),
            ((52, 190, 176), "estimated covered"),
        ],
        [
            ((238, 177, 61), "next viewpoint"),
            ((229, 94, 94), "unreachable target"),
        ],
        [
            ((104, 166, 244), "robot now"),
            ((170, 249, 235), "stopped observation pose"),
        ],
    ]
    for legend_row, entries in enumerate(legend_rows):
        legend_x = 16
        legend_y = 66 + legend_row * 28
        for color, label in entries:
            draw.rectangle((legend_x, legend_y + 1, legend_x + 14, legend_y + 15), fill=color)
            draw.text((legend_x + 20, legend_y), label, fill=(190, 200, 213))
            legend_x += 20 + len(label) * 7 + 30

    for row in range(plan.height):
        canvas_y = top + (plan.height - 1 - row) * cell_px
        for col in range(plan.width):
            index = row * plan.width + col
            if not plan.traversable[row, col]:
                color = (20, 23, 29)
            elif not plan.reachable[row, col]:
                color = (29, 33, 41)
            elif index in covered:
                color = (52, 190, 176)
            elif not plan.navigable[row, col]:
                color = (38, 44, 54)
            else:
                color = (47, 55, 67)
            canvas_x = col * cell_px
            draw.rectangle((canvas_x, canvas_y, canvas_x + cell_px - 1, canvas_y + cell_px - 1), fill=color)

    for observation in state["observations"]:
        _draw_pose_marker(draw, plan, observation, cell_px, top, (170, 249, 235))
    for unreachable in state["unreachable"]:
        _draw_pose_marker(draw, plan, unreachable, cell_px, top, (229, 94, 94), cross=True)
    if target is not None:
        _draw_pose_marker(
            draw,
            plan,
            {"x": target.x, "y": target.y, "theta": target.theta},
            cell_px,
            top,
            (238, 177, 61),
        )
    _draw_pose_marker(
        draw,
        plan,
        {"x": pose.x, "y": pose.y, "theta": pose.theta},
        cell_px,
        top,
        (104, 166, 244),
    )
    draw.text(
        (16, top + map_height + 20),
        "Estimated coverage is a static-map ray-cast from stopped poses; dynamic visual occluders are not modeled.",
        fill=(151, 163, 180),
    )

    png = io.BytesIO()
    canvas.save(png, format="PNG", optimize=True)
    jpeg = io.BytesIO()
    canvas.save(jpeg, format="JPEG", quality=88, optimize=True)
    reachable_indices = np.flatnonzero(reachable.reshape(-1)).tolist()
    metadata = {
        "version": 1,
        "status": status,
        "coverage_model": "static_occupancy_raycast",
        "map_fingerprint": state.get("map_fingerprint"),
        "coverage_fraction": coverage,
        "covered_count": len(covered),
        "reachable_count": reachable_count,
        "grid": {
            "width": plan.width,
            "height": plan.height,
            "resolution": plan.resolution,
            "origin_x": plan.origin_x,
            "origin_y": plan.origin_y,
            "origin_theta": plan.origin_theta,
        },
        "reachable_cells": reachable_indices,
        "navigable_viewpoint_cells": np.flatnonzero(plan.navigable.reshape(-1)).tolist(),
        "covered_cells": sorted(covered),
        "uncovered_cells": sorted(set(reachable_indices).difference(covered)),
        "observations": state["observations"],
        "unreachable": state["unreachable"],
        "robot_pose": {"x": pose.x, "y": pose.y, "theta": pose.theta},
        "target": (
            {"x": target.x, "y": target.y, "theta": target.theta, "new_cells": target.gain}
            if target is not None
            else None
        ),
    }
    write_artifact_set(
        "exploration",
        {
            "exploration_coverage.png": png.getvalue(),
            "exploration_coverage.json": json.dumps(metadata, indent=2).encode(),
            "exploration_status.json": json.dumps(
                {"status": status, "coverage_artifact_current": True}, indent=2
            ).encode(),
        },
    )
    return jpeg.getvalue()


def _placeholder_artifact(
    status: str,
    headline: str,
    detail: str,
    *,
    coverage_fraction: float | None,
) -> bytes | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    canvas = Image.new("RGB", (760, 260), (17, 20, 26))
    draw = ImageDraw.Draw(canvas)
    draw.text((24, 26), "HOUSEHOLD EXPLORATION COVERAGE", fill=(245, 247, 250))
    draw.text((24, 80), headline, fill=(87, 221, 204))
    draw.text((24, 112), detail, fill=(166, 177, 194))
    png = io.BytesIO()
    canvas.save(png, format="PNG", optimize=True)
    jpeg = io.BytesIO()
    canvas.save(jpeg, format="JPEG", quality=88, optimize=True)
    write_artifact_set(
        "exploration",
        {
            "exploration_coverage.png": png.getvalue(),
            "exploration_coverage.json": json.dumps(
                {"version": 1, "status": status, "coverage_fraction": coverage_fraction}, indent=2
            ).encode(),
            "exploration_status.json": json.dumps(
                {"status": status, "coverage_artifact_current": False, "detail": detail}, indent=2
            ).encode(),
        },
    )
    return jpeg.getvalue()


def _preserve_coverage_artifact(status: str, headline: str, detail: str) -> bytes | None:
    """Record transient failure without destroying the last valid map snapshot."""
    artifact_dir = artifact_root()
    write_artifact_set(
        "exploration_status",
        {
            "exploration_status.json": json.dumps(
                {"status": status, "coverage_artifact_current": False, "detail": detail}, indent=2
            ).encode()
        },
    )
    png_path = artifact_dir / "exploration_coverage.png"
    metadata_path = artifact_dir / "exploration_coverage.json"
    if png_path.is_file() and metadata_path.is_file():
        try:
            from PIL import Image

            with Image.open(png_path) as source:
                preview = io.BytesIO()
                source.convert("RGB").save(preview, format="JPEG", quality=88, optimize=True)
            return preview.getvalue()
        except Exception:
            pass
    return _placeholder_artifact(status, headline, detail, coverage_fraction=None)


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "run_id": None,
        "map_fingerprint": None,
        "observations": [],
        "unreachable": [],
        "navigation_failures": [],
        "recovery_anchor": None,
        "recovery_rejections": [],
    }


def _read_state(raw) -> dict:
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return _empty_state()
    observations = raw.get("observations")
    unreachable = raw.get("unreachable")
    if not isinstance(observations, list) or not isinstance(unreachable, list):
        return _empty_state()
    navigation_failures = raw.get("navigation_failures", [])
    if not isinstance(navigation_failures, list):
        navigation_failures = []
    recovery_rejections = raw.get("recovery_rejections", [])
    if not isinstance(recovery_rejections, list):
        recovery_rejections = []
    return {
        "version": STATE_VERSION,
        "run_id": raw.get("run_id") if isinstance(raw.get("run_id"), str) else None,
        "map_fingerprint": raw.get("map_fingerprint"),
        # Mission coverage is monotonic until reset/map change. These entries
        # are small pose records; dropping old ones would make searched floor
        # become unseen again and could create an endless cycle.
        "observations": observations,
        "unreachable": unreachable,
        "navigation_failures": navigation_failures,
        # Set only after an accepted route moved away from a planner-valid
        # source pose and then failed. It is an internal escape hatch, not a
        # target exposed to the agent.
        "recovery_anchor": raw.get("recovery_anchor") if isinstance(raw.get("recovery_anchor"), dict) else None,
        # A recovery target is one-shot. Keeping rejected reached poses here
        # prevents one locally blocked anchor from monopolizing exploration.
        "recovery_rejections": recovery_rejections,
    }


class FindNextPerson(Skill):
    """Move to one safe unseen-map viewpoint and return a fresh camera image.

    ``reset`` clears persisted coverage. ``visualize`` returns the current
    coverage artifact without moving.
    """

    map: Map | None
    pose: Pose | None
    image: MainImage | None
    head: Head
    head_position: HeadState | None
    navigate: NavigateToPosition

    def _refresh_placeholder(self, status: str, headline: str, detail: str) -> bytes | None:
        try:
            return _placeholder_artifact(status, headline, detail, coverage_fraction=0.0)
        except Exception as error:
            self.logger.warning(f"[FindNextPerson] could not refresh visualization: {error}")
            return None

    def _preserve_visualization(self, status: str, headline: str, detail: str) -> bytes | None:
        try:
            return _preserve_coverage_artifact(status, headline, detail)
        except Exception as error:
            self.logger.warning(f"[FindNextPerson] could not record visualization status: {error}")
            return None

    def _refresh_visualization(
        self,
        plan: _PlanningGrid,
        state: dict,
        covered: set[int],
        pose: Pose,
        *,
        status: str,
        target: _View | None = None,
    ) -> bytes | None:
        try:
            return _coverage_artifact(plan, state, covered, pose, status=status, target=target)
        except Exception as error:
            self.logger.warning(f"[FindNextPerson] could not refresh visualization: {error}")
            return None

    def _fresh_upward_frame(self) -> MainImage | None:
        """Raise the low-mounted camera, settle, then capture a new frame."""
        current_head = self.wait_for(lambda: self.head_position, timeout=HEAD_SETTLE_TIMEOUT_S)
        if current_head is None:
            return None

        reported_max = current_head.max_degrees
        target = PERSON_VIEW_HEAD_PITCH_DEG
        if reported_max is not None and math.isfinite(reported_max):
            target = min(target, reported_max)
        command = int(round(target))
        feedback_before_command = current_head.raw_source
        self.head.set_position(command)

        settled = self.wait_for(
            lambda: (
                self.head_position
                if self.head_position is not None
                and (feedback_before_command is None or self.head_position.raw_source is not feedback_before_command)
                and abs(self.head_position.pitch_degrees - command) <= HEAD_POSITION_TOLERANCE_DEG
                else None
            ),
            timeout=HEAD_SETTLE_TIMEOUT_S,
        )
        if settled is None:
            return None

        # The frame present when feedback first says "settled" may have been
        # exposed while the servo was still moving. Require the next camera
        # object so the attached evidence is unambiguously upward-looking.
        settled_frame = self.image
        first_post_settle_frame = self.wait_for(
            lambda: self.image if self.image is not None and self.image is not settled_frame else None,
            timeout=FRAME_TIMEOUT_S,
        )
        if first_post_settle_frame is None:
            return None
        # Head feedback and camera delivery use independent subscriptions. A
        # frame delivered just after feedback may still have been exposed just
        # before it; one additional frame interval establishes the new gaze.
        return self.wait_for(
            lambda: self.image if self.image is not None and self.image is not first_post_settle_frame else None,
            timeout=FRAME_TIMEOUT_S,
        )

    def _recover_to_previous_viewpoint(
        self,
        plan: _PlanningGrid,
        state: dict,
        covered: set[int],
        pose: Pose,
    ) -> SkillReturn:
        """Use collision-checked local Nav2 to leave a source pose global Nav2 rejects."""
        target = _recovery_target(plan, state, pose)
        if target is None:
            state["recovery_anchor"] = None
            state["recovery_rejections"] = []
            self.storage["state"] = state
            self._refresh_visualization(
                plan,
                state,
                covered,
                pose,
                status="route recovery unavailable",
            )
            return _result(
                "SEARCH_INFRASTRUCTURE_FAILURE",
                {
                    "component": "navigation",
                    "reason": "the route planner rejected the current source pose and no nearby safe reached viewpoint was available",
                },
            )

        local_x, local_y, local_theta = _relative_pose_goal(pose, target)
        try:
            self.navigate.navigate_relative(
                x=local_x,
                y=local_y,
                theta_degrees=math.degrees(local_theta),
            )
        except SkillCancelled:
            self._refresh_visualization(
                plan,
                state,
                covered,
                self.pose or pose,
                status="interrupted during route recovery",
            )
            raise
        except SkillFailed as error:
            state["recovery_rejections"].append(
                {"x": round(target[0], 4), "y": round(target[1], 4), "theta": round(target[2], 5)}
            )
            next_target = _recovery_target(plan, state, self.pose or pose)
            state["recovery_anchor"] = (
                None
                if next_target is None
                else {"x": round(next_target[0], 4), "y": round(next_target[1], 4), "theta": round(next_target[2], 5)}
            )
            if next_target is None:
                state["recovery_rejections"] = []
            self.storage["state"] = state
            self._refresh_visualization(
                plan,
                state,
                covered,
                self.pose or pose,
                status="route recovery failed",
            )
            return _result(
                "SEARCH_INFRASTRUCTURE_FAILURE",
                {
                    "component": "navigation",
                    "reason": f"navigation to a previously reached viewpoint failed: {error}",
                },
            )

        state["recovery_anchor"] = None
        state["recovery_rejections"] = []
        self.storage["state"] = state
        recovered_pose = self.pose or pose
        try:
            fresh_frame = self._fresh_upward_frame()
        except SkillCancelled:
            self._refresh_visualization(
                plan,
                state,
                covered,
                recovered_pose,
                status="interrupted after route recovery",
            )
            raise
        self._refresh_visualization(
            plan,
            state,
            covered,
            recovered_pose,
            status="returned to a previously reached viewpoint; coverage unchanged",
        )
        if fresh_frame is None:
            return _result("CAMERA_UNAVAILABLE", {"phase": "after_recovery", "coverage_recorded": False})
        return _result(
            "SEARCH_OBSERVATION",
            {"coverage_recorded": False, "navigation": "recovered"},
            image=fresh_frame.jpeg,
        )

    def execute(self, reset: bool = False, visualize: bool = False) -> SkillReturn:
        if reset:
            run_id = active_run_id()
            if run_id is None:
                return _result("RUN_UNINITIALIZED")
            state = _read_state(self.storage.get("state"))
            if state.get("run_id") == run_id:
                return _result(
                    "SEARCH_ALREADY_INITIALIZED",
                    {
                        "observations": len(state["observations"]),
                        "unreachable": len(state["unreachable"]),
                    },
                )
            state = _empty_state()
            state["run_id"] = run_id
            self.storage["state"] = state
            self._refresh_placeholder(
                "reset",
                "Mission coverage reset",
                "The map will appear when find_next_person selects its first viewpoint.",
            )
            return _result("SEARCH_RESET")

        map_state = self.wait_for(lambda: self.map, timeout=FRAME_TIMEOUT_S)
        if map_state is None or map_state.grid is None:
            self._preserve_visualization(
                "map unavailable",
                "Map unavailable",
                "No coverage snapshot can be trusted until a readable occupancy map returns.",
            )
            return _result("MAP_UNAVAILABLE", {"reason": "no_readable_map"})
        pose = self.wait_for(lambda: self.pose, timeout=FRAME_TIMEOUT_S)
        if pose is None:
            self._preserve_visualization(
                "pose unavailable",
                "Robot pose unavailable",
                "No coverage snapshot can be placed on the map until localization returns.",
            )
            return _result("POSE_UNAVAILABLE", {"reason": "no_localized_pose"})
        cells = map_state.grid
        plan = _build_planning_grid(map_state, pose)
        if plan is None:
            self._preserve_visualization(
                "map unusable",
                "Map has no reachable known-free floor",
                "The planner could not construct a trustworthy exploration grid.",
            )
            return _result("MAP_UNAVAILABLE", {"reason": "no_reachable_known_free_floor"})
        state = _read_state(self.storage.get("state"))
        handled_person_anchors = _handled_person_anchors()
        fingerprint = _map_fingerprint(map_state, cells)
        if state["map_fingerprint"] != fingerprint:
            run_id = state.get("run_id")
            state = _empty_state()
            state["run_id"] = run_id
            state["map_fingerprint"] = fingerprint
            self.storage["state"] = state

        anchor = _pose_record(state.get("recovery_anchor"))
        if anchor is not None and math.hypot(anchor[0] - pose.x, anchor[1] - pose.y) < RECOVERY_MIN_DISTANCE_M:
            state["recovery_anchor"] = None
            state["recovery_rejections"] = []
            self.storage["state"] = state

        if visualize:
            if plan.navigable.any():
                try:
                    view, covered = _choose_view(
                        plan,
                        pose,
                        state["observations"],
                        _navigation_exclusions(state),
                        handled_person_anchors,
                        cancellation_check=self.check_cancelled,
                    )
                except SkillCancelled:
                    self._refresh_visualization(
                        plan,
                        state,
                        _covered_cells(plan, state["observations"]),
                        self.pose or pose,
                        status="interrupted while preparing visualization",
                    )
                    raise
                status = "preview"
            else:
                view = None
                covered = _covered_cells(plan, state["observations"])
                status = "preview; no safe reachable viewpoint"
            coverage = _coverage_fraction(plan, covered)
            preview = self._refresh_visualization(plan, state, covered, pose, status=status, target=view)
            return _result(
                "SEARCH_VISUALIZATION",
                {
                    "coverage_fraction": coverage,
                    "observations": len(state["observations"]),
                    "unreachable": len(state["unreachable"]),
                    "artifact_available": preview is not None,
                    "image_path": str(ARTIFACT_RELATIVE_DIR / "exploration_coverage.png"),
                    "metadata_path": str(ARTIFACT_RELATIVE_DIR / "exploration_coverage.json"),
                },
                image=preview,
            )

        covered = _covered_cells(plan, state["observations"])
        if not plan.navigable.any():
            self._refresh_visualization(
                plan,
                state,
                covered,
                pose,
                status="no safe reachable viewpoint",
            )
            return _result("MAP_UNAVAILABLE", {"reason": "no_safe_reachable_viewpoint"})

        if self.wait_for(lambda: self.image, timeout=FRAME_TIMEOUT_S) is None:
            self._refresh_visualization(
                plan,
                state,
                covered,
                pose,
                status="camera unavailable; coverage unchanged",
            )
            return _result("CAMERA_UNAVAILABLE", {"phase": "before_navigation"})

        try:
            view, covered = _choose_view(
                plan,
                pose,
                state["observations"],
                _navigation_exclusions(state),
                handled_person_anchors,
                cancellation_check=self.check_cancelled,
            )
        except SkillCancelled:
            self._refresh_visualization(
                plan,
                state,
                _covered_cells(plan, state["observations"]),
                self.pose or pose,
                status="interrupted while choosing a viewpoint",
            )
            raise
        coverage = _coverage_fraction(plan, covered)
        if view is None:
            if state.get("recovery_anchor") is not None:
                return self._recover_to_previous_viewpoint(plan, state, covered, pose)
            self._refresh_visualization(plan, state, covered, pose, status="search exhausted")
            return _result(
                "SEARCH_EXHAUSTED",
                {
                    "coverage_fraction": coverage,
                    "observations": len(state["observations"]),
                    "unreachable": len(state["unreachable"]),
                },
            )

        self._refresh_visualization(plan, state, covered, pose, status="navigating to next viewpoint", target=view)

        def refresh_interrupted(status: str) -> None:
            current_pose = self.pose or pose
            current_covered = _covered_cells(plan, state["observations"])
            self._refresh_visualization(
                plan,
                state,
                current_covered,
                current_pose,
                status=status,
                target=view,
            )

        stopped_short = False
        expected_heading = view.theta
        translated = False
        try:
            rotation = _rotation_at_reached_viewpoint(
                view,
                pose,
                allow_visual_standoff=bool(state["observations"]) and state.get("recovery_anchor") is None,
            )
            if rotation is None:
                translated = True
                stopped_short = self.navigate.approach_viewpoint(
                    x=round(view.x, 4),
                    y=round(view.y, 4),
                    theta_degrees=round(math.degrees(view.theta), 2),
                    stop_within_m=VIEWPOINT_STANDOFF_M,
                )
                if stopped_short:
                    approach_pose = self.wait_for(
                        lambda: self.pose if self.pose is not None and self.pose.stamp > pose.stamp else None,
                        timeout=FRAME_TIMEOUT_S,
                    )
                    if approach_pose is None:
                        raise SkillFailed("pose unavailable after visual standoff")
                    expected_heading = _standoff_heading(view, approach_pose)
                    self.navigate.rotate_in_place(math.degrees(_wrap_angle(expected_heading - approach_pose.theta)))
            else:
                stopped_short = math.hypot(view.x - pose.x, view.y - pose.y) > ARRIVAL_POSITION_TOLERANCE_M
                self.navigate.rotate_in_place(rotation)
        except SkillCancelled:
            refresh_interrupted("interrupted during navigation")
            raise
        except NavigationPathIndeterminateError:
            if state.get("recovery_anchor") is not None:
                return self._recover_to_previous_viewpoint(plan, state, covered, pose)
            confirmed_unreachable = _record_navigation_failure(state, view)
            self.storage["state"] = state
            refresh_interrupted(
                "viewpoint unreachable" if confirmed_unreachable else "viewpoint route needs confirmation"
            )
            return _result("SEARCH_UNREACHABLE")
        except NavigationInfrastructureError as error:
            refresh_interrupted("navigation temporarily unavailable")
            return _result(
                "SEARCH_INFRASTRUCTURE_FAILURE",
                {"component": "navigation", "reason": str(error)},
            )
        except NavigationExecutionError:
            try:
                self.check_cancelled()
            except SkillCancelled:
                refresh_interrupted("interrupted during navigation")
                raise
            _defer_navigation_execution_failure(state, view)
            # A resident is a dynamic obstacle and can be the very reason the
            # planned viewpoint became unreachable.  Inspect where Nav2
            # stopped instead of throwing away the strongest close-person
            # signal. A preflight getPath rejection never moved, however, so
            # do not relabel the old camera view as if it came from the failed
            # goal. Coverage remains unchanged either way.
            stopped_pose = self.pose
            self._refresh_visualization(
                plan,
                state,
                covered,
                stopped_pose or pose,
                status="route failed; viewpoint temporarily deferred",
            )
            moved = stopped_pose is not None and (
                math.hypot(stopped_pose.x - pose.x, stopped_pose.y - pose.y) >= 0.15
                or _angular_distance(stopped_pose.theta, pose.theta) >= math.radians(10.0)
            )
            if moved:
                # This source pose passed global Nav2 preflight immediately
                # before the accepted route failed, making it a stronger
                # recovery anchor than an inferred nearest-free grid cell.
                state["recovery_anchor"] = {
                    "x": round(pose.x, 4),
                    "y": round(pose.y, 4),
                    "theta": round(pose.theta, 5),
                }
                state["recovery_rejections"] = []
            self.storage["state"] = state
            try:
                fresh_frame = self._fresh_upward_frame() if moved else None
            except SkillCancelled:
                refresh_interrupted("interrupted while inspecting a failed viewpoint")
                raise
            if fresh_frame is not None and stopped_pose is not None:
                return _result(
                    "SEARCH_OBSERVATION",
                    {
                        "coverage_recorded": False,
                        "navigation": "unreachable",
                    },
                    image=fresh_frame.jpeg,
                )
            return _result("SEARCH_UNREACHABLE", {"temporary": True})
        except SkillFailed as error:
            try:
                self.check_cancelled()
            except SkillCancelled:
                refresh_interrupted("interrupted during navigation")
                raise
            refresh_interrupted("navigation integration failed")
            return _result(
                "SEARCH_INFRASTRUCTURE_FAILURE",
                {"component": "navigation", "reason": str(error)},
            )

        if _clear_navigation_failure(state, view):
            self.storage["state"] = state
        if translated and state.get("recovery_anchor") is not None:
            state["recovery_anchor"] = None
            state["recovery_rejections"] = []
            self.storage["state"] = state

        # Nav2 intentionally pitches the head down while moving. A person close
        # to this 26cm-high camera would otherwise be reduced to shins, so every
        # visual-coverage stop deliberately looks up before it counts.
        try:
            fresh_frame = self._fresh_upward_frame()
        except SkillCancelled:
            refresh_interrupted("interrupted while raising the camera")
            raise
        if fresh_frame is None:
            self._refresh_visualization(
                plan,
                state,
                covered,
                self.pose or pose,
                status="camera unavailable after navigation",
            )
            return _result("CAMERA_UNAVAILABLE", {"phase": "after_navigation", "coverage_recorded": False})

        try:
            position_tolerance = (
                VIEWPOINT_STANDOFF_M + VIEWPOINT_STANDOFF_TOLERANCE_M if stopped_short else ARRIVAL_POSITION_TOLERANCE_M
            )
            observed_pose = self.wait_for(
                lambda: (
                    self.pose
                    if self.pose is not None
                    and self.pose.stamp > pose.stamp
                    and math.hypot(self.pose.x - view.x, self.pose.y - view.y) <= position_tolerance
                    and _angular_distance(self.pose.theta, expected_heading) <= ARRIVAL_HEADING_TOLERANCE_RAD
                    else None
                ),
                timeout=FRAME_TIMEOUT_S,
            )
        except SkillCancelled:
            refresh_interrupted("interrupted while verifying the observation pose")
            raise
        if observed_pose is None:
            self._refresh_visualization(
                plan,
                state,
                covered,
                self.pose or pose,
                status="pose unavailable after observation",
            )
            return _result("POSE_UNAVAILABLE", {"phase": "after_navigation", "coverage_recorded": False})
        observation = {
            "x": round(observed_pose.x, 4),
            "y": round(observed_pose.y, 4),
            "theta": round(observed_pose.theta, 5),
            "target_x": round(view.x, 4),
            "target_y": round(view.y, 4),
            "target_theta": round(view.theta, 5),
            "stopped_short": stopped_short,
        }
        state["observations"].append(observation)
        self.storage["state"] = state

        updated_covered = set(covered)
        row, col = _world_to_cell(plan, observed_pose.x, observed_pose.y)
        if 0 <= row < plan.height and 0 <= col < plan.width:
            updated_covered.update(_visible_cells(plan, row, col, observed_pose.theta))
        updated_coverage = _coverage_fraction(plan, updated_covered)
        self._refresh_visualization(
            plan,
            state,
            updated_covered,
            observed_pose,
            status=f"observation {len(state['observations'])} committed",
        )
        return _result(
            "SEARCH_OBSERVATION",
            {
                "coverage_fraction": updated_coverage,
                "observations": len(state["observations"]),
                "coverage_recorded": True,
            },
            image=fresh_frame.jpeg,
        )
