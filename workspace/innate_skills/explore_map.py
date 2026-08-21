# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Autonomously build a map by navigating to reachable exploration frontiers.

Use when the user explicitly asks the robot to map or explore a space. The
skill switches into autonomous mapping, then lets slam_toolbox own the growing
map while Nav2 performs collision-aware motion. It never commands wheel
velocities directly. Manual joystick input, Stop, a mode change, or stale map,
pose, or lidar data ends exploration immediately.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from brain_messages.srv import ChangeNavigationMode
from geometry_msgs.msg import Twist
from innate_skills._frontier_planner import (
    FrontierPlanningError,
    GridGeometry,
    find_frontier_goals,
    known_area_m2,
)
from innate_skills.navigate_to_position import Nav2Controller

from innate import Lidar, Map, NavMode, Pose, Skill, SkillCancelled, SkillFailed, SkillOutput, SkillReturn, resource

AUTONOMOUS_MAPPING_MODE = "autonomous_mapping"
MODE_CHANGE_TIMEOUT_S = 60.0
INITIAL_STATE_TIMEOUT_S = 12.0
MAP_STALE_TIMEOUT_S = 4.0
POSE_STALE_TIMEOUT_S = 2.0
LIDAR_STALE_TIMEOUT_S = 1.5
MAX_CONSECUTIVE_FAILURES = 3
STABLE_EMPTY_MAPS = 2


def _sample_marker(sample: object) -> tuple[str, object]:
    """Track source time when available, identity only for unstamped fakes.

    RobotState memoizes each ROS sample, but `/mapping_pose` itself is a
    wrapper around TF. Its TF-derived stamp is the authority that proves SLAM
    is still updating; merely receiving a newly allocated wrapper is not.
    """
    stamp = getattr(sample, "stamp", None)
    if isinstance(stamp, (int, float)) and stamp > 0.0:
        return ("stamp", float(stamp))
    return ("identity", id(sample))


class _MappingModeController:
    def __init__(self, skill: ExploreMap):
        self.skill = skill
        self.client = skill.node.create_client(ChangeNavigationMode, "/nav/change_mode")

    def switch_to_autonomous_mapping(self) -> None:
        deadline = time.monotonic() + MODE_CHANGE_TIMEOUT_S
        while not self.client.service_is_ready():
            if time.monotonic() >= deadline:
                raise SkillFailed("navigation mode service is unavailable")
            self.skill.sleep(0.1)

        request = ChangeNavigationMode.Request()
        request.mode = AUTONOMOUS_MAPPING_MODE
        future = self.client.call_async(request)
        try:
            while not future.done():
                if time.monotonic() >= deadline:
                    future.cancel()
                    raise SkillFailed("timed out starting autonomous mapping mode")
                self.skill.sleep(0.1)
            response = future.result()
        except (SkillCancelled, SkillFailed):
            future.cancel()
            raise
        except Exception as exc:
            raise SkillFailed(f"navigation mode service failed: {exc}") from exc
        if response is None or not response.success:
            detail = response.message if response is not None else "no response"
            raise SkillFailed(f"could not start autonomous mapping mode: {detail}")

    def destroy(self) -> None:
        self.skill.node.destroy_client(self.client)


class _TeleopTakeover:
    """A non-zero human drive command cancels, rather than merely masks, Nav2."""

    def __init__(self, skill: ExploreMap):
        self.taken_over = False
        self.armed = False
        self._skill = skill
        self._sub = skill.node.create_subscription(Twist, "/cmd_vel_teleop", self._on_command, 10)
        self._node = skill.node

    def _on_command(self, msg: Twist) -> None:
        moving = abs(msg.linear.x) > 1e-3 or abs(msg.linear.y) > 1e-3 or abs(msg.angular.z) > 1e-3
        if self.armed and moving and not self.taken_over:
            self.taken_over = True
            # Cancel from the subscription callback so takeover is immediate
            # even during a mode-service or initial-state wait. This also fires
            # Nav2Controller's external-action cancel hook when a goal is live.
            self._skill.cancel()

    def destroy(self) -> None:
        self._node.destroy_subscription(self._sub)


class ExploreMap(Skill):
    nav_mode: NavMode
    map: Map | None = None
    pose: Pose | None = None
    lidar: Lidar | None = None

    @resource
    def mode_controller(self) -> Iterator[_MappingModeController]:
        controller = _MappingModeController(self)
        yield controller
        controller.destroy()

    @resource
    def teleop(self) -> Iterator[_TeleopTakeover]:
        monitor = _TeleopTakeover(self)
        yield monitor
        monitor.destroy()

    @resource
    def navigator(self) -> Iterator[Nav2Controller]:
        controller = Nav2Controller(self)
        yield controller
        controller.destroy()

    def guidelines(self) -> str:
        return (
            "Autonomously explore a space to build its occupancy map. Use only after an explicit request to map. "
            "The skill starts autonomous mapping itself; do not precede it with MoveStraight or raw driving. "
            "The user can keep talking while it runs and can say Stop or use the joystick to take over."
        )

    def guidelines_when_running(self) -> str:
        return (
            "Mapping continues while you answer ordinary questions. Report progress conversationally without stopping it. "
            "Stop only when the user explicitly asks, manually drives, or a safety condition ends the run."
        )

    def _wait_for_autonomous_mode(self) -> NavMode:
        mode = self.wait_for(
            lambda: self.nav_mode if self.nav_mode.value == AUTONOMOUS_MAPPING_MODE else None,
            timeout=INITIAL_STATE_TIMEOUT_S,
        )
        if mode is None:
            raise SkillFailed(f"mode manager did not enter {AUTONOMOUS_MAPPING_MODE}")
        return mode

    def _wait_for_mapping_state(
        self,
        previous_map: Map | None,
        previous_pose: Pose | None,
        require_new_state: bool,
    ) -> tuple[Map, Pose, Lidar]:
        map_state = self.wait_for(
            lambda: (
                self.map if self.map is not None and (not require_new_state or self.map is not previous_map) else None
            ),
            timeout=INITIAL_STATE_TIMEOUT_S,
        )
        pose = self.wait_for(
            lambda: (
                self.pose
                if self.pose is not None and (not require_new_state or self.pose is not previous_pose)
                else None
            ),
            timeout=INITIAL_STATE_TIMEOUT_S,
        )
        lidar = self.wait_for(lambda: self.lidar, timeout=INITIAL_STATE_TIMEOUT_S)
        if map_state is None:
            raise SkillFailed("no fresh live SLAM map arrived after the mode switch")
        if pose is None:
            detail = (
                "no fresh map-frame mapping pose arrived after the mode switch"
                if require_new_state
                else ("no map-frame mapping pose is available")
            )
            raise SkillFailed(detail)
        if lidar is None:
            raise SkillFailed("no lidar scan is available for obstacle avoidance")
        if map_state.frame_id != "map" or pose.frame_id != "map":
            raise SkillFailed(
                f"mapping frame mismatch: map={map_state.frame_id!r}, pose={pose.frame_id!r}; both must be 'map'"
            )
        return map_state, pose, lidar

    def _arm_safety(self, map_state: Map, pose: Pose, lidar: Lidar) -> None:
        now = time.monotonic()
        self._last_map_marker = _sample_marker(map_state)
        self._last_map_seen_at = now
        self._last_pose_marker = _sample_marker(pose)
        self._last_pose_seen_at = now
        self._last_lidar_marker = _sample_marker(lidar)
        self._last_lidar_seen_at = now
        self.teleop.armed = True

    def _safety_check(self) -> None:
        self.check_cancelled()
        if self.nav_mode.value != AUTONOMOUS_MAPPING_MODE:
            raise SkillCancelled(f"mapping stopped because navigation mode changed to {self.nav_mode.value}")
        if self.teleop.taken_over:
            raise SkillCancelled("mapping stopped for manual joystick takeover")

        now = time.monotonic()
        current_map = self.map
        current_pose = self.pose
        current_lidar = self.lidar
        map_marker = _sample_marker(current_map) if current_map is not None else None
        pose_marker = _sample_marker(current_pose) if current_pose is not None else None
        lidar_marker = _sample_marker(current_lidar) if current_lidar is not None else None
        if map_marker is not None and map_marker != self._last_map_marker:
            self._last_map_marker = map_marker
            self._last_map_seen_at = now
        if pose_marker is not None and pose_marker != self._last_pose_marker:
            self._last_pose_marker = pose_marker
            self._last_pose_seen_at = now
        if lidar_marker is not None and lidar_marker != self._last_lidar_marker:
            self._last_lidar_marker = lidar_marker
            self._last_lidar_seen_at = now
        if now - self._last_map_seen_at > MAP_STALE_TIMEOUT_S:
            raise SkillCancelled("mapping stopped because the live SLAM map stopped updating")
        if now - self._last_pose_seen_at > POSE_STALE_TIMEOUT_S:
            raise SkillCancelled("mapping stopped because the map-frame pose stopped updating")
        if now - self._last_lidar_seen_at > LIDAR_STALE_TIMEOUT_S:
            raise SkillCancelled("mapping stopped because lidar scans stopped updating")

    def execute(
        self,
        max_duration_minutes: float = 10.0,
        max_frontiers: int = 30,
        goal_timeout_seconds: float = 90.0,
    ) -> SkillReturn:
        if not 1.0 <= float(max_duration_minutes) <= 60.0:
            self.fail("max_duration_minutes must be between 1 and 60")
        if isinstance(max_frontiers, bool) or not 1 <= int(max_frontiers) <= 100:
            self.fail("max_frontiers must be between 1 and 100")
        if not 10.0 <= float(goal_timeout_seconds) <= 300.0:
            self.fail("goal_timeout_seconds must be between 10 and 300")

        max_duration_s = float(max_duration_minutes) * 60.0
        max_frontiers = int(max_frontiers)
        goal_timeout_seconds = float(goal_timeout_seconds)
        # Construct and arm the subscription before the potentially long mode
        # transition, so manual input during startup also owns the base.
        self.teleop.armed = True
        started_at = time.monotonic()
        deadline = started_at + max_duration_s
        previous_map = self.map
        previous_pose = self.pose
        changed_mode = self.nav_mode.value != AUTONOMOUS_MAPPING_MODE

        self.feedback("Starting SLAM and collision-aware navigation for autonomous mapping")
        if changed_mode:
            try:
                self.mode_controller.switch_to_autonomous_mapping()
            except SkillFailed as exc:
                self.fail(str(exc))
        try:
            self._wait_for_autonomous_mode()
            initial_map, initial_pose, initial_lidar = self._wait_for_mapping_state(
                previous_map,
                previous_pose,
                changed_mode,
            )
        except SkillFailed as exc:
            self.fail(str(exc))
        self._arm_safety(initial_map, initial_pose, initial_lidar)

        start_grid = initial_map.grid
        if start_grid is None:
            self.fail("live SLAM map has no occupancy data")
        start_area = known_area_m2(start_grid, initial_map.resolution)
        excluded_goals: list[tuple[float, float]] = []
        reached = 0
        failures = 0
        consecutive_failures = 0
        stable_empty_maps = 0
        last_empty_map = None
        stop_reason = "frontier limit reached"
        planning_error_since = None

        while reached < max_frontiers:
            self._safety_check()
            if time.monotonic() >= deadline:
                stop_reason = "time limit reached"
                break

            map_state = self.map
            pose = self.pose
            if map_state is None or pose is None or map_state.frame_id != "map" or pose.frame_id != "map":
                raise SkillCancelled("mapping stopped because map-frame state became unavailable")
            grid = map_state.grid
            if grid is None:
                raise SkillCancelled("mapping stopped because occupancy data became unavailable")
            geometry = GridGeometry(
                resolution=map_state.resolution,
                origin_x=map_state.origin_x,
                origin_y=map_state.origin_y,
                origin_yaw=map_state.origin_theta,
            )
            try:
                goals = find_frontier_goals(
                    grid,
                    geometry,
                    pose.x,
                    pose.y,
                    excluded_world=excluded_goals,
                    check_cancelled=self._safety_check,
                )
                planning_error_since = None
            except FrontierPlanningError as exc:
                if planning_error_since is None:
                    planning_error_since = time.monotonic()
                    self.feedback(f"Waiting for SLAM to settle: {exc}")
                elif time.monotonic() - planning_error_since > INITIAL_STATE_TIMEOUT_S:
                    self.fail(f"could not plan mapping frontiers: {exc}")
                self.sleep(0.5)
                continue

            if not goals:
                if map_state is not last_empty_map:
                    stable_empty_maps += 1
                    last_empty_map = map_state
                if stable_empty_maps >= STABLE_EMPTY_MAPS:
                    stop_reason = "no reachable frontiers remain" if failures else "map coverage complete"
                    break
                self.sleep(0.5)
                continue
            stable_empty_maps = 0
            last_empty_map = None

            goal = goals[0]
            remaining_s = max(0.0, deadline - time.monotonic())
            timeout_s = min(goal_timeout_seconds, remaining_s)
            if timeout_s < 1.0:
                stop_reason = "time limit reached"
                break
            self.feedback(
                f"Exploring frontier {reached + 1}/{max_frontiers} at ({goal.x:.2f}, {goal.y:.2f}); "
                f"route {goal.route_distance_m:.1f}m"
            )
            try:
                self.navigator.go_to_position(
                    goal.x,
                    goal.y,
                    goal.yaw,
                    local_frame=False,
                    timeout_s=timeout_s,
                    safety_check=self._safety_check,
                )
            except SkillFailed as exc:
                failures += 1
                consecutive_failures += 1
                excluded_goals.append((goal.x, goal.y))
                self.feedback(
                    f"Frontier was unreachable; replanning ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {exc}"
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    stop_reason = "navigation failed repeatedly"
                    break
                self.sleep(0.5)
                continue

            reached += 1
            consecutive_failures = 0
            excluded_goals.append((goal.x, goal.y))
            self.feedback(f"Reached frontier {reached}; updating the exploration plan")
            self.sleep(0.5)

        self._safety_check()
        end_map = self.map or initial_map
        end_grid = end_map.grid
        end_area = known_area_m2(end_grid, end_map.resolution) if end_grid is not None else start_area
        elapsed = time.monotonic() - started_at
        if stop_reason == "navigation failed repeatedly" and reached == 0:
            self.fail(f"Autonomous mapping could not reach a frontier after {failures} navigation failures")
        return SkillOutput(
            f"Autonomous mapping stopped: {stop_reason}. Reached {reached} frontiers and mapped "
            f"{end_area:.1f} m² ({max(0.0, end_area - start_area):.1f} m² new).",
            data={
                "reached_frontiers": reached,
                "navigation_failures": failures,
                "start_known_area_m2": round(start_area, 3),
                "end_known_area_m2": round(end_area, 3),
                "duration_s": round(elapsed, 1),
                "stop_reason": stop_reason,
                "mode": AUTONOMOUS_MAPPING_MODE,
            },
        )
