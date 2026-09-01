# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time
from collections.abc import Iterator

from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from rclpy.qos import DurabilityPolicy, QoSProfile

from innate import Map, Odometry, Pose, Skill, SkillCancelled, SkillFailed, SkillReturn, resource

# Frame local (robot-relative) goals are resolved into before being sent to
# Nav2. Must match the mapfree costmap's global_frame (mars_nav costmap.yaml)
# and Odometry.frame_id, which is what resolves them.
LOCAL_GOAL_FIXED_FRAME = "odom"
APPROACH_SEARCH_STEPS = 5
MIN_APPROACH_PROGRESS_M = 0.15


def resolve_local_goal(base_x, base_y, base_yaw, x, y, theta):
    """Compose a base_link-relative (x, y, theta) goal with the robot's pose in
    the fixed frame, returning (gx, gy, gyaw) expressed in that fixed frame."""
    gx = base_x + x * math.cos(base_yaw) - y * math.sin(base_yaw)
    gy = base_y + x * math.sin(base_yaw) + y * math.cos(base_yaw)
    return gx, gy, base_yaw + theta


def _map_cell(map_state: Map, x: float, y: float) -> tuple[int, int] | None:
    """Return the occupancy-grid cell containing a map-frame point."""
    dx = x - map_state.origin_x
    dy = y - map_state.origin_y
    cos_map = math.cos(-map_state.origin_theta)
    sin_map = math.sin(-map_state.origin_theta)
    col = math.floor((dx * cos_map - dy * sin_map) / map_state.resolution)
    row = math.floor((dx * sin_map + dy * cos_map) / map_state.resolution)
    if 0 <= row < map_state.height and 0 <= col < map_state.width:
        return row, col
    return None


def _map_goal_issue(map_state: Map | None, x: float, y: float) -> str | None:
    """Return a cause we can prove from the map, without guessing at Nav2 errors."""
    if map_state is None or map_state.grid is None:
        return None
    cell = _map_cell(map_state, x, y)
    if cell is None:
        return "the target is outside the current map"
    row, col = cell
    keepouts = map_state.keepout_grid
    if keepouts is not None and keepouts[row, col] >= 50:
        return "the target is inside a keepout zone"
    if map_state.grid[row, col] >= 50:
        return "the target is an occupied map cell"
    return None


def _has_keepouts(map_state: Map | None) -> bool:
    keepouts = map_state.keepout_grid if map_state is not None else None
    return keepouts is not None and bool((keepouts >= 50).any())


def _keepout_distance_ahead(map_state: Map | None, x0: float, y0: float, x1: float, y1: float) -> float | None:
    """How far along the straight line to the target the first keepout cell
    sits, or None if that line stays clear. A geometric fact about the direct
    line, not a claim about the planner's own route."""
    keepouts = map_state.keepout_grid if map_state is not None else None
    if keepouts is None:
        return None
    distance = math.hypot(x1 - x0, y1 - y0)
    if distance <= 0.0:
        return None
    steps = max(1, math.ceil(distance / (map_state.resolution * 0.5)))
    for step in range(steps + 1):
        fraction = step / steps
        cell = _map_cell(map_state, x0 + (x1 - x0) * fraction, y0 + (y1 - y0) * fraction)
        if cell is not None and keepouts[cell] >= 50:
            return fraction * distance
    return None


class Nav2Controller:
    def __init__(self, skill):
        self.skill = skill
        self.logger = skill.logger
        self.navigator = BasicNavigator(namespace="")
        self.navigator_mapfree = BasicNavigator(namespace="mapfree")
        self.navigator_navigation = BasicNavigator(namespace="navigation")

        # The exact goal this skill commands, latched so UIs can render the
        # true target (the replanned path's endpoint wiggles).
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._commanded_goal_pub = self.navigator.create_publisher(PoseStamped, "/nav/commanded_goal", latched)

    def _resolve_goal(self, x, y, theta, local_frame):
        if not local_frame:
            return x, y, theta, "map"
        # self.skill.odom is the odom->base_link pose the goal is relative to,
        # injected and kept current by the framework — so there is no TF buffer
        # to warm up here, which is what made a freshly built controller's
        # first local goal racy.
        base = self.skill.odom
        gx, gy, gyaw = resolve_local_goal(base.x, base.y, base.theta, x, y, theta)
        self.logger.info(f"Resolved local goal ({x}, {y}, {theta}) to ({gx:.3f}, {gy:.3f}, {gyaw:.3f})")
        return gx, gy, gyaw, LOCAL_GOAL_FIXED_FRAME

    def _pose_stamped(self, x: float, y: float, yaw: float, frame: str) -> PoseStamped:
        pose = PoseStamped()
        pose.header.frame_id = frame
        pose.header.stamp = self.navigator.get_clock().now().to_msg()
        pose.pose.position.x = x
        pose.pose.position.y = y
        pose.pose.orientation.z = math.sin(yaw / 2.0)
        pose.pose.orientation.w = math.cos(yaw / 2.0)
        return pose

    def _start_xy(self, local_frame: bool) -> tuple[float, float] | None:
        state = self.skill.odom if local_frame else getattr(self.skill, "pose", None)
        if state is None:
            return None
        return float(state.x), float(state.y)

    def _closest_reachable_approach(
        self,
        path_navigator: BasicNavigator,
        goal_x: float,
        goal_y: float,
        goal_yaw: float,
        goal_frame: str,
        local_frame: bool,
    ) -> tuple[float, float, float] | None:
        """Find a reachable approach on the current-pose-to-goal line.

        This is a bounded best-effort hint for the agent, not a command. Five
        bisection probes cap the extra planner work while locating the approach
        to roughly 1/32 of the original distance.
        """
        start = self._start_xy(local_frame)
        if start is None:
            return None
        start_x, start_y = start
        total_distance = math.hypot(goal_x - start_x, goal_y - start_y)
        if total_distance < MIN_APPROACH_PROGRESS_M:
            return None

        reachable_fraction = 0.0
        blocked_fraction = 1.0
        for _ in range(APPROACH_SEARCH_STEPS):
            self.skill.check_cancelled()
            fraction = (reachable_fraction + blocked_fraction) / 2.0
            candidate_x = start_x + fraction * (goal_x - start_x)
            candidate_y = start_y + fraction * (goal_y - start_y)
            candidate = self._pose_stamped(candidate_x, candidate_y, goal_yaw, goal_frame)
            if path_navigator.getPath(candidate, candidate, use_start=False) is None:
                blocked_fraction = fraction
            else:
                reachable_fraction = fraction

        progressed = reachable_fraction * total_distance
        if progressed < MIN_APPROACH_PROGRESS_M:
            return None
        approach_x = start_x + reachable_fraction * (goal_x - start_x)
        approach_y = start_y + reachable_fraction * (goal_y - start_y)
        return approach_x, approach_y, total_distance - progressed

    def _no_path_detail(
        self,
        path_navigator: BasicNavigator,
        goal_x: float,
        goal_y: float,
        goal_yaw: float,
        goal_frame: str,
        local_frame: bool,
    ) -> str:
        map_state = getattr(self.skill, "map", None) if not local_frame else None
        start = self._start_xy(local_frame)
        issue = _map_goal_issue(map_state, goal_x, goal_y)
        blocked_at = (
            _keepout_distance_ahead(map_state, start[0], start[1], goal_x, goal_y) if start is not None else None
        )
        if issue is not None:
            reason = issue
        elif blocked_at is not None:
            reason = f"a keepout zone crosses the direct line to the target, {blocked_at:.2f}m ahead"
        elif _has_keepouts(map_state):
            reason = "active keepout zones may cut off the route"
        else:
            reason = "the route may be blocked or the target may be unreachable"

        detail = f"the planner found no path because {reason}"
        approach = self._closest_reachable_approach(path_navigator, goal_x, goal_y, goal_yaw, goal_frame, local_frame)
        if approach is None:
            if start is not None:
                remaining = math.hypot(goal_x - start[0], goal_y - start[1])
                detail += (
                    f". The robot did not move and remains at ({start[0]:.2f}, {start[1]:.2f}), "
                    f"{remaining:.2f}m from the target"
                )
            else:
                detail += ". The robot did not move"
        else:
            approach_x, approach_y, remaining = approach
            detail += (
                f". A reachable approach on the direct line is ({approach_x:.2f}, {approach_y:.2f}) "
                f"in the {goal_frame} frame, {remaining:.2f}m short of the target. "
                "The robot did not move there; navigate to that pose explicitly if approaching is appropriate"
            )
        return detail

    def go_to_position(self, x: float, y: float, theta: float, local_frame: bool) -> None:
        """Navigate to the goal, blocking until Nav2 finishes. Raises
        SkillFailed with a human-readable reason; a skill cancel unwinds as
        SkillCancelled with the Nav2 task cancelled."""
        goal_x, goal_y, goal_yaw, goal_frame = self._resolve_goal(x, y, theta, local_frame)

        goal_pose = self._pose_stamped(goal_x, goal_y, goal_yaw, goal_frame)
        self._commanded_goal_pub.publish(goal_pose)

        path_navigator = self.navigator_mapfree if local_frame else self.navigator_navigation
        if path_navigator.getPath(goal_pose, goal_pose, use_start=False) is None:
            raise SkillFailed(self._no_path_detail(path_navigator, goal_x, goal_y, goal_yaw, goal_frame, local_frame))

        self.navigator.goToPose(goal_pose, behavior_tree="mapfree" if local_frame else "navigation")

        initial_distance = -1.0
        last_distance = -1.0
        last_recoveries = 0
        last_progress_log = 0.0
        said_close_to_goal = False
        last_pose = None
        while not self.navigator.isTaskComplete():
            try:
                self.skill.sleep(0.1)
            except SkillCancelled:
                self.navigator.cancelTask()
                raise

            feedback = self.navigator.getFeedback()
            if not feedback:
                continue
            # Nav2's own distance_remaining: feedback.current_pose is in the
            # navigator's global frame while our goal may be in another.
            distance = feedback.distance_remaining
            last_recoveries = feedback.number_of_recoveries
            current = feedback.current_pose
            last_pose = (current.pose.position.x, current.pose.position.y, current.header.frame_id or "map")
            if distance > 0.0:
                last_distance = distance
                if initial_distance < 0.0:
                    initial_distance = distance

            now = time.monotonic()
            if now - last_progress_log >= 1.0:
                last_progress_log = now
                completion = 100.0 * (1.0 - distance / initial_distance) if initial_distance > 0.0 else 0.0
                self.logger.info(
                    f"Navigation progress: {max(0.0, min(100.0, completion)):.0f}% "
                    f"({distance:.2f}m remaining, {last_recoveries} recoveries)"
                )

            if 0.0 < distance < 0.2 and not said_close_to_goal:
                said_close_to_goal = True
                self.skill.feedback(
                    "I'm almost done with this movement, if I think I should navigate again to pursue this task"
                    ", I should stop the current primitive and start a new navigation movement."
                )

        result = self.navigator.getResult()
        if result == TaskResult.SUCCEEDED:
            return
        detail = f"Nav2 reported {getattr(result, 'name', result)}"
        if last_distance >= 0.0:
            detail += f" with {last_distance:.2f}m still to go"
        if last_pose is not None:
            detail += f"; the robot stopped at ({last_pose[0]:.2f}, {last_pose[1]:.2f}) in the {last_pose[2]} frame"
        if last_recoveries > 0:
            detail += f" after {last_recoveries} recovery attempt{'s' if last_recoveries != 1 else ''}"
        map_state = getattr(self.skill, "map", None) if not local_frame else None
        issue = _map_goal_issue(map_state, goal_x, goal_y)
        if issue is not None:
            detail += f"; {issue}"
        elif _has_keepouts(map_state):
            detail += "; active keepout zones may block the route"
        else:
            detail += "; the route may be blocked or the robot may be stuck"
        raise SkillFailed(detail + ". The target was not reached")

    def destroy(self):
        """Destroy the navigator nodes so their graph entities disappear now,
        not at some eventual GC pass."""
        for navigator in (self.navigator, self.navigator_mapfree, self.navigator_navigation):
            try:
                # Humble's BasicNavigator.destroy_node() misses this client; its
                # live handle would keep the rcl node and graph entities alive.
                navigator.assisted_teleop_client.destroy()
            except Exception as e:
                self.logger.warning(f"Error destroying assisted_teleop client: {e}")
            try:
                navigator.destroy_node()
            except Exception as e:
                self.logger.warning(f"Error destroying navigator node: {e}")


class NavigateToPosition(Skill):
    """Use when you need to navigate the robot to the specified position
    using provided x, y coordinates (meters), and theta_degrees (yaw) IN DEGREES.
    If local_frame is set to false, it navigates to a specific point in the map.
    If local_frame is set to true, it navigates locally, where the robot is currently (0,0)"""

    odom: Odometry
    """Resolves local_frame goals; see Nav2Controller._resolve_goal."""
    map: Map | None
    """Classifies absolute-map planning failures, including keepout targets."""
    pose: Pose | None
    """Reports the current map pose and seeds closest-approach planning."""

    @resource
    def controller(self) -> Iterator[Nav2Controller]:
        controller = Nav2Controller(self)
        yield controller
        controller.destroy()

    def guidelines(self):
        return (
            "Move the robot to a position given x, y (meters) and theta_degrees (yaw IN DEGREES). "
            "Prefer local_frame=true: coordinates are then relative to where the robot stands now — "
            "robot at (0,0) facing theta=0, x forward, y left (e.g. turn around: x=0, y=0, theta_degrees=180). "
            "Use local_frame=false only to reach absolute coordinates in the frame your pose is reported in."
        )

    def execute(
        self, x: float, y: float, theta_degrees: float = 0.0, local_frame: bool = False, **legacy
    ) -> SkillReturn:
        # The tool schema speaks theta_degrees, but the cloud agent and the
        # pose-adjustment pipeline speak `theta` in radians.
        if legacy.get("theta") is not None:
            theta = float(legacy["theta"])
            theta_degrees = math.degrees(theta)
        else:
            theta = math.radians(theta_degrees)
        self.logger.info(f"Navigating to x={x}, y={y}, theta_degrees={theta_degrees}, local_frame={local_frame}")

        goal_desc = f"({x}, {y}, {theta_degrees} deg, {'local' if local_frame else 'map'} frame)"
        try:
            self.controller.go_to_position(x, y, theta, local_frame)
        except SkillFailed as e:
            self.fail(f"Navigation to {goal_desc} failed: {e}")
        return f"Reached position ({x}, {y}, {theta_degrees} deg)"
