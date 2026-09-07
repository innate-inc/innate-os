# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import threading
import time
import uuid
from collections.abc import Iterator

from action_msgs.msg import GoalStatus
from action_msgs.srv import CancelGoal
from builtin_interfaces.msg import Duration
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import ComputePathToPose, NavigateToPose, Spin
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile
from unique_identifier_msgs.msg import UUID

from innate import Map, Mobility, Odometry, Pose, Skill, SkillCancelled, SkillFailed, SkillReturn, resource

# Frame local (robot-relative) goals are resolved into before being sent to
# Nav2. Must match the mapfree costmap's global_frame (mars_nav costmap.yaml)
# and Odometry.frame_id, which is what resolves them.
LOCAL_GOAL_FIXED_FRAME = "odom"
KEEPOUT_GOAL_ERROR = "the target is inside a keepout zone"
APPROACH_SEARCH_STEPS = 5
MIN_APPROACH_PROGRESS_M = 0.15
PATH_SERVER_TIMEOUT_S = 2.0
PATH_RESULT_TIMEOUT_S = 10.0
NAVIGATION_SERVER_TIMEOUT_S = 2.0
NAVIGATION_GOAL_RESPONSE_TIMEOUT_S = 3.0
NAVIGATION_CANCEL_TIMEOUT_S = 4.0
NAVIGATION_EXECUTOR_SHUTDOWN_TIMEOUT_S = 1.0
NAVIGATION_STALL_TIMEOUT_S = 15.0
NAVIGATION_MAX_DURATION_S = 90.0
ROTATION_MAX_DURATION_S = 15.0
NAVIGATION_PROGRESS_EPSILON_M = 0.08
NAVIGATE_ACTION_NAME = "navigate_to_pose"
SPIN_ACTION_NAME = "spin"


class NavigationInfrastructureError(SkillFailed):
    """A temporary Nav2 action failure that says nothing about goal reachability."""


class NavigationPathIndeterminateError(NavigationInfrastructureError):
    """A completed preflight that Humble cannot classify as geometric or transient."""


class NavigationExecutionError(SkillFailed):
    """An accepted Nav2 goal that failed because this route could not be executed."""


class NavigationNoProgressError(NavigationExecutionError):
    """An accepted Nav2 goal that kept reporting distance but made no progress."""


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
        return KEEPOUT_GOAL_ERROR
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


def map_goal_error(map_state: Map, x: float, y: float) -> str | None:
    """Explain why an absolute goal is not known-free, or return ``None``."""
    grid = map_state.grid
    if grid is None or grid.ndim != 2 or not grid.size or map_state.resolution <= 0:
        return "the current occupancy map is unreadable"

    dx = x - map_state.origin_x
    dy = y - map_state.origin_y
    cos_o = math.cos(map_state.origin_theta)
    sin_o = math.sin(map_state.origin_theta)
    local_x = cos_o * dx + sin_o * dy
    local_y = -sin_o * dx + cos_o * dy
    col = int(math.floor(local_x / map_state.resolution))
    row = int(math.floor(local_y / map_state.resolution))
    if not (0 <= row < grid.shape[0] and 0 <= col < grid.shape[1]):
        return "the goal is outside the current occupancy map"

    keepouts = map_state.keepout_grid
    if keepouts is not None and keepouts[row, col] >= 50:
        return KEEPOUT_GOAL_ERROR

    occupancy = int(grid[row, col])
    if occupancy < 0:
        return "the goal is in unknown space, not confirmed floor"
    if occupancy >= 50:
        return "the goal is occupied on the current map"
    return None


class Nav2Controller:
    def __init__(self, skill):
        self.skill = skill
        self.logger = skill.logger
        self.navigator = BasicNavigator(namespace="")
        self.navigator_mapfree = BasicNavigator(namespace="mapfree")
        self.navigator_navigation = BasicNavigator(namespace="navigation")
        self._navigator_nodes = (self.navigator, self.navigator_mapfree, self.navigator_navigation)

        # The exact goal this skill commands, latched so UIs can render the
        # true target (the replanned path's endpoint wiggles).
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._commanded_goal_pub = self.navigator.create_publisher(PoseStamped, "/nav/commanded_goal", latched)
        self._navigate_cancel_client = self.navigator.create_client(
            CancelGoal, f"{NAVIGATE_ACTION_NAME}/_action/cancel_goal"
        )
        self._navigate_result_client = self.navigator.create_client(
            NavigateToPose.Impl.GetResultService,
            f"{NAVIGATE_ACTION_NAME}/_action/get_result",
        )
        self._spin_cancel_client = self.navigator.create_client(CancelGoal, f"{SPIN_ACTION_NAME}/_action/cancel_goal")
        self._spin_result_client = self.navigator.create_client(
            Spin.Impl.GetResultService,
            f"{SPIN_ACTION_NAME}/_action/get_result",
        )
        # rclpy's process-global executor cannot be spun concurrently by a
        # cancellation guardian and a later skill invocation. Each controller
        # therefore owns one executor for all three of its navigator nodes.
        self._executor_spin_lock = threading.Lock()
        self._executor = SingleThreadedExecutor(context=self.navigator.context)
        for navigator in self._navigator_nodes:
            self._executor.add_node(navigator)
        self._active_goal_uuid: UUID | None = None
        self._active_cancel_client = None
        self._active_result_client = None
        self._active_result_service = None
        self._active_send_future = None
        self._cancel_guardian: threading.Thread | None = None
        self._cancel_guardian_done = threading.Event()
        self._guardian_lock = threading.Lock()
        self._destroy_deferred = False
        self._destroy_lock = threading.Lock()
        self._navigators_destroyed = False

    def _spin_once(self, _navigator: BasicNavigator, timeout_sec: float = 0.0) -> None:
        with self._executor_spin_lock:
            if self._navigators_destroyed:
                raise NavigationInfrastructureError("the Nav2 controller has already been released")
            self._executor.spin_once(timeout_sec=timeout_sec)

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

    def _map_frame_goal(self, goal_x: float, goal_y: float, goal_frame: str) -> tuple[float, float] | None:
        """The commanded goal as a point in the map frame, where the map and
        keepout mask live.

        Read at diagnosis time, never latched at goal time: `pose` is cleared
        between skill runs, so at goal time it is routinely still None and a
        latched map goal would be None for the whole run.
        """
        if goal_frame != LOCAL_GOAL_FIXED_FRAME:
            return goal_x, goal_y
        pose = getattr(self.skill, "pose", None)
        odom = self.skill.odom
        if pose is None or odom is None:
            return None
        # the odom goal is fixed, so composing it with the odom->map offset read
        # now stays correct after the recoveries have moved the robot
        angle = pose.theta - odom.theta
        dx, dy = goal_x - odom.x, goal_y - odom.y
        return (
            pose.x + dx * math.cos(angle) - dy * math.sin(angle),
            pose.y + dx * math.sin(angle) + dy * math.cos(angle),
        )

    def _blocking_cause(self, goal_x: float, goal_y: float, goal_frame: str) -> str | None:
        """A cause proven from the map and keepout grids, in the map frame."""
        map_state = getattr(self.skill, "map", None)
        if map_state is None:
            return None
        map_goal = self._map_frame_goal(goal_x, goal_y, goal_frame)
        pose = getattr(self.skill, "pose", None)
        if map_goal is not None:
            issue = _map_goal_issue(map_state, *map_goal)
            if issue is not None:
                return issue
            blocked_at = _keepout_distance_ahead(map_state, pose.x, pose.y, *map_goal) if pose is not None else None
            if blocked_at is not None:
                return f"a keepout zone crosses the direct line to the target, {blocked_at:.2f}m ahead"
        if _has_keepouts(map_state):
            return "active keepout zones may cut off the route"
        return None

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
            map_candidate = self._map_frame_goal(candidate_x, candidate_y, goal_frame)
            if map_candidate is not None and _map_goal_issue(getattr(self.skill, "map", None), *map_candidate):
                blocked_fraction = fraction
                continue
            candidate = self._pose_stamped(candidate_x, candidate_y, goal_yaw, goal_frame)
            try:
                reachable = self._path_exists(path_navigator, candidate)
            except NavigationPathIndeterminateError:
                # An aborted probe is not evidence that a point is reachable.
                # Keep the hint conservative without classifying the original
                # failure as geometric rather than a temporary planner error.
                reachable = False
            except NavigationInfrastructureError:
                # A best-effort hint must not hide the original failure or
                # continue probing an unavailable planner.
                return None
            if reachable:
                reachable_fraction = fraction
            else:
                blocked_fraction = fraction

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
        *,
        refusal_reason: str | None = None,
    ) -> str:
        start = self._start_xy(local_frame)
        reason = (
            refusal_reason
            or self._blocking_cause(goal_x, goal_y, goal_frame)
            or "the route may be blocked or the target may be unreachable"
        )
        detail = (
            f"navigation was refused because {reason}"
            if refusal_reason is not None
            else f"the planner did not produce a path; {reason}"
        )
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

    def _path_exists(self, navigator: BasicNavigator, goal_pose: PoseStamped) -> bool:
        """Run Nav2's path preflight without its unbounded future waits."""
        client = navigator.compute_path_to_pose_client
        if not client.wait_for_server(timeout_sec=PATH_SERVER_TIMEOUT_S):
            raise NavigationInfrastructureError("the path planner action server is unavailable")

        goal = ComputePathToPose.Goal()
        goal.start = goal_pose
        goal.goal = goal_pose
        goal.use_start = False
        self.logger.info("Checking route with Nav2")
        send_future = client.send_goal_async(goal)
        deadline = time.monotonic() + PATH_RESULT_TIMEOUT_S
        while not send_future.done():
            try:
                self.skill.sleep(0.05)
            except SkillCancelled:
                send_future.cancel()
                raise
            self._spin_once(navigator, timeout_sec=0.0)
            if time.monotonic() >= deadline:
                send_future.cancel()
                raise NavigationInfrastructureError(
                    f"the path planner did not respond within {PATH_RESULT_TIMEOUT_S:.0f}s"
                )

        try:
            goal_handle = send_future.result()
        except Exception as error:
            raise NavigationInfrastructureError("the path planner goal request failed") from error
        if goal_handle is None:
            raise NavigationInfrastructureError("the path planner returned no goal handle")
        if not goal_handle.accepted:
            raise NavigationInfrastructureError("the path planner rejected the route request")
        try:
            result_future = goal_handle.get_result_async()
        except Exception as error:
            raise NavigationInfrastructureError("the path planner result request failed") from error
        deadline = time.monotonic() + PATH_RESULT_TIMEOUT_S
        while not result_future.done():
            try:
                self.skill.sleep(0.05)
            except SkillCancelled:
                goal_handle.cancel_goal_async()
                raise
            self._spin_once(navigator, timeout_sec=0.0)
            if time.monotonic() >= deadline:
                goal_handle.cancel_goal_async()
                raise NavigationInfrastructureError(
                    f"the path planner did not respond within {PATH_RESULT_TIMEOUT_S:.0f}s"
                )

        try:
            result = result_future.result()
        except Exception as error:
            raise NavigationInfrastructureError("the path planner result failed") from error
        if result is None:
            raise NavigationInfrastructureError("the path planner returned no result")
        if result.status == GoalStatus.STATUS_SUCCEEDED:
            return True
        if result.status == GoalStatus.STATUS_ABORTED:
            # Humble's ComputePathToPose result has no planner error code. The
            # server uses ABORTED both for a genuine no-path result and for
            # transient TF/planner exceptions, so let the composed search
            # require a repeat before suppressing the viewpoint.
            raise NavigationPathIndeterminateError("the path planner aborted the route request")
        raise NavigationInfrastructureError(f"the path planner ended with action status {result.status}")

    def _wait_for_future(
        self,
        navigator: BasicNavigator,
        future,
        timeout_s: float,
        timeout_message: str,
        *,
        cancellable: bool = True,
        cancel_on_timeout: bool = True,
    ):
        """Wait for a ROS future using a finite, cancellation-aware polling loop."""
        deadline = time.monotonic() + timeout_s
        while not future.done():
            if cancellable:
                self.skill.sleep(0.05)
                self._spin_once(navigator, timeout_sec=0.0)
            else:
                self._spin_once(navigator, timeout_sec=min(0.05, max(0.0, deadline - time.monotonic())))
            if future.done():
                break
            if time.monotonic() >= deadline:
                if cancel_on_timeout:
                    future.cancel()
                raise NavigationInfrastructureError(timeout_message)
        try:
            return future.result()
        except Exception as error:
            raise NavigationInfrastructureError(timeout_message) from error

    @staticmethod
    def _new_goal_uuid() -> UUID:
        return UUID(uuid=list(uuid.uuid4().bytes))

    @staticmethod
    def _same_goal_uuid(first: UUID, second: UUID) -> bool:
        return bytes(first.uuid) == bytes(second.uuid)

    def _set_active_action(
        self,
        *,
        goal_uuid: UUID,
        cancel_client,
        result_client,
        result_service,
    ) -> None:
        self._active_goal_uuid = goal_uuid
        self._active_cancel_client = cancel_client
        self._active_result_client = result_client
        self._active_result_service = result_service
        self._active_send_future = None
        self.navigator.goal_handle = None
        self.navigator.result_future = None
        self.navigator.status = None

    def _clear_active_action(self) -> None:
        self._active_goal_uuid = None
        self._active_cancel_client = None
        self._active_result_client = None
        self._active_result_service = None
        self._active_send_future = None

    def _brake_base(self) -> None:
        """Publish the configured best-effort emergency zero command."""
        try:
            self.skill.mobility.stop()
        except Exception as error:
            self.logger.warning(f"Could not publish emergency zero command: {error}")

    def _cancel_failure(self, message: str) -> NavigationInfrastructureError:
        return NavigationInfrastructureError(message)

    def _request_terminal_result(self, deadline: float, timeout_message: str):
        result_client = self._active_result_client
        result_service = self._active_result_service
        goal_uuid = self._active_goal_uuid
        if result_client is None or result_service is None or goal_uuid is None:
            raise self._cancel_failure("Nav2 lost the active goal result endpoint")
        remaining = deadline - time.monotonic()
        if remaining <= 0.0 or not result_client.wait_for_service(timeout_sec=min(0.5, remaining)):
            raise self._cancel_failure(timeout_message)
        request = result_service.Request()
        request.goal_id = goal_uuid
        try:
            result_future = result_client.call_async(request)
        except Exception as error:
            raise self._cancel_failure("Nav2 terminal result request failed") from error
        self.navigator.result_future = result_future
        return self._wait_for_future(
            self.navigator,
            result_future,
            max(0.0, deadline - time.monotonic()),
            timeout_message,
            cancellable=False,
            cancel_on_timeout=False,
        )

    def _cancel_navigation_exact(self, timeout_message: str) -> None:
        """Cancel the exact active UUID and require a terminal action state."""
        goal_uuid = self._active_goal_uuid
        cancel_client = self._active_cancel_client
        if goal_uuid is None or cancel_client is None:
            raise self._cancel_failure("Nav2 lost the active goal cancellation endpoint")

        result_future = self.navigator.result_future
        if result_future is not None and result_future.done():
            try:
                result = result_future.result()
            except Exception:
                result = None
                self.navigator.result_future = None
            if result is not None and result.status in (
                GoalStatus.STATUS_SUCCEEDED,
                GoalStatus.STATUS_ABORTED,
                GoalStatus.STATUS_CANCELED,
            ):
                self.navigator.status = result.status
                self._clear_active_action()
                return

        deadline = time.monotonic() + NAVIGATION_CANCEL_TIMEOUT_S
        cancellation_accepted = False
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if not cancel_client.wait_for_service(timeout_sec=min(0.5, remaining)):
                self._brake_base()
                self._spin_once(self.navigator, timeout_sec=min(0.05, max(0.0, remaining)))
                continue
            request = CancelGoal.Request()
            request.goal_info.goal_id = goal_uuid
            try:
                cancel_future = cancel_client.call_async(request)
                response = self._wait_for_future(
                    self.navigator,
                    cancel_future,
                    max(0.0, deadline - time.monotonic()),
                    timeout_message,
                    cancellable=False,
                )
            except NavigationInfrastructureError:
                break
            if response is None:
                break
            if response.return_code == CancelGoal.Response.ERROR_GOAL_TERMINATED:
                self._clear_active_action()
                return
            if response.return_code == CancelGoal.Response.ERROR_NONE:
                cancellation_accepted = any(
                    self._same_goal_uuid(info.goal_id, goal_uuid) for info in response.goals_canceling
                )
                if cancellation_accepted:
                    break
                raise self._cancel_failure("Nav2 returned an empty or mismatched cancellation acknowledgement")
            if response.return_code == CancelGoal.Response.ERROR_UNKNOWN_GOAL_ID:
                self._brake_base()
                self._spin_once(self.navigator, timeout_sec=min(0.05, max(0.0, deadline - time.monotonic())))
                continue
            raise self._cancel_failure(f"Nav2 rejected cancellation with code {response.return_code}")

        if not cancellation_accepted:
            raise self._cancel_failure(timeout_message)

        result = self.navigator.result_future
        if result is None:
            terminal = self._request_terminal_result(deadline, timeout_message)
        else:
            terminal = self._wait_for_future(
                self.navigator,
                result,
                max(0.0, deadline - time.monotonic()),
                timeout_message,
                cancellable=False,
                cancel_on_timeout=False,
            )
        if terminal is None or terminal.status not in (
            GoalStatus.STATUS_SUCCEEDED,
            GoalStatus.STATUS_ABORTED,
            GoalStatus.STATUS_CANCELED,
        ):
            raise self._cancel_failure("Nav2 cancellation did not reach a terminal action state")
        self.navigator.status = terminal.status
        self._clear_active_action()

    def _refresh_pending_goal_handle(self) -> bool:
        """Return True only when a delayed submission is confirmed rejected."""
        send_future = self._active_send_future
        if send_future is None or not send_future.done():
            return False
        try:
            goal_handle = send_future.result()
        except Exception:
            return False
        if goal_handle is None:
            return False
        if not goal_handle.accepted:
            self._clear_active_action()
            return True
        self.navigator.goal_handle = goal_handle
        if self.navigator.result_future is None:
            try:
                self.navigator.result_future = goal_handle.get_result_async()
            except Exception:
                pass
        return False

    def _guard_active_goal(self) -> None:
        """Own the standalone navigator until a delayed goal is terminal."""
        try:
            while self._active_goal_uuid is not None:
                if self._refresh_pending_goal_handle():
                    break
                try:
                    self._cancel_navigation_exact("Nav2 cancellation guardian has not reached a terminal state")
                except Exception as error:
                    self._brake_base()
                    self.logger.warning(f"Nav2 cancellation guardian retrying: {error}")
                    # Deliberately non-cancellable teardown: this daemon owns
                    # the action clients until the exact UUID is terminal.
                    time.sleep(0.1)
        finally:
            with self._guardian_lock:
                try:
                    if self._destroy_deferred:
                        self._destroy_navigators()
                finally:
                    self._cancel_guardian_done.set()

    def _start_cancel_guardian(self) -> None:
        with self._guardian_lock:
            guardian = self._cancel_guardian
            if guardian is not None and guardian.is_alive():
                return
            self._cancel_guardian_done.clear()
            guardian = threading.Thread(
                target=self._guard_active_goal,
                name="nav2-cancel-guardian",
                daemon=True,
            )
            self._cancel_guardian = guardian
            guardian.start()

    def _cancel_navigation(self, timeout_message: str) -> None:
        try:
            self._cancel_navigation_exact(timeout_message)
        except NavigationInfrastructureError:
            self._brake_base()
            if self._active_goal_uuid is not None:
                self._start_cancel_guardian()
            raise
        except Exception as error:
            self._brake_base()
            if self._active_goal_uuid is not None:
                self._start_cancel_guardian()
            raise NavigationInfrastructureError("Nav2 cancellation verification failed") from error

    def _cancel_interrupted_submission(self, send_future, timeout_message: str) -> None:
        """Cancel a submitted UUID even when its goal-response future is still pending."""
        if send_future.done():
            try:
                goal_handle = send_future.result()
            except Exception:
                goal_handle = None
            if goal_handle is not None and not goal_handle.accepted:
                self._clear_active_action()
                return
            if goal_handle is not None:
                self.navigator.goal_handle = goal_handle
                try:
                    self.navigator.result_future = goal_handle.get_result_async()
                except Exception:
                    self.navigator.result_future = None
        self._cancel_navigation(timeout_message)

    def _start_navigation(self, goal_pose: PoseStamped, behavior_tree: str) -> None:
        """Submit NavigateToPose without BasicNavigator's unbounded synchronous waits."""
        client = self.navigator.nav_to_pose_client
        if not client.wait_for_server(timeout_sec=NAVIGATION_SERVER_TIMEOUT_S):
            raise NavigationInfrastructureError("the NavigateToPose action server is unavailable")
        self.skill.sleep(0.0)

        goal = NavigateToPose.Goal()
        goal.pose = goal_pose
        goal.behavior_tree = behavior_tree
        goal_uuid = self._new_goal_uuid()
        self._set_active_action(
            goal_uuid=goal_uuid,
            cancel_client=self._navigate_cancel_client,
            result_client=self._navigate_result_client,
            result_service=NavigateToPose.Impl.GetResultService,
        )
        self.navigator.feedback = None
        try:
            send_future = client.send_goal_async(goal, self.navigator._feedbackCallback, goal_uuid=goal_uuid)
            self._active_send_future = send_future
        except Exception as error:
            self._clear_active_action()
            raise NavigationInfrastructureError("NavigateToPose goal request failed") from error
        try:
            goal_handle = self._wait_for_future(
                self.navigator,
                send_future,
                NAVIGATION_GOAL_RESPONSE_TIMEOUT_S,
                f"NavigateToPose did not accept the goal within {NAVIGATION_GOAL_RESPONSE_TIMEOUT_S:.0f}s",
                cancel_on_timeout=False,
            )
        except SkillCancelled:
            self._cancel_interrupted_submission(
                send_future,
                "NavigateToPose did not confirm interrupted-goal cancellation",
            )
            raise
        except NavigationInfrastructureError:
            self._cancel_interrupted_submission(
                send_future,
                "NavigateToPose did not confirm timed-out-goal cancellation",
            )
            raise
        if goal_handle is None:
            self._cancel_navigation("NavigateToPose returned no handle and cancellation was not confirmed")
            raise NavigationInfrastructureError("NavigateToPose returned no goal handle")
        if not goal_handle.accepted:
            self._clear_active_action()
            raise NavigationInfrastructureError("NavigateToPose rejected the goal")
        try:
            result_future = goal_handle.get_result_async()
        except Exception as error:
            self.navigator.goal_handle = goal_handle
            self.navigator.result_future = None
            self._cancel_navigation("NavigateToPose did not confirm cancellation after losing its result")
            raise NavigationInfrastructureError("NavigateToPose could not request its result") from error
        self.navigator.goal_handle = goal_handle
        self.navigator.result_future = result_future
        self.navigator.status = None

    def _start_rotation(self, theta: float) -> None:
        """Submit Spin without BasicNavigator.spin's unbounded synchronous waits."""
        client = self.navigator.spin_client
        if not client.wait_for_server(timeout_sec=NAVIGATION_SERVER_TIMEOUT_S):
            raise NavigationInfrastructureError("the Spin action server is unavailable")
        self.skill.sleep(0.0)

        goal = Spin.Goal()
        goal.target_yaw = theta
        goal.time_allowance = Duration(sec=10)
        goal_uuid = self._new_goal_uuid()
        self._set_active_action(
            goal_uuid=goal_uuid,
            cancel_client=self._spin_cancel_client,
            result_client=self._spin_result_client,
            result_service=Spin.Impl.GetResultService,
        )
        self.navigator.feedback = None
        try:
            send_future = client.send_goal_async(goal, self.navigator._feedbackCallback, goal_uuid=goal_uuid)
            self._active_send_future = send_future
        except Exception as error:
            self._clear_active_action()
            raise NavigationInfrastructureError("Spin goal request failed") from error
        try:
            goal_handle = self._wait_for_future(
                self.navigator,
                send_future,
                NAVIGATION_GOAL_RESPONSE_TIMEOUT_S,
                f"Spin did not accept the goal within {NAVIGATION_GOAL_RESPONSE_TIMEOUT_S:.0f}s",
                cancel_on_timeout=False,
            )
        except SkillCancelled:
            self._cancel_interrupted_submission(send_future, "Spin did not confirm interrupted-goal cancellation")
            raise
        except NavigationInfrastructureError:
            self._cancel_interrupted_submission(send_future, "Spin did not confirm timed-out-goal cancellation")
            raise
        if goal_handle is None:
            self._cancel_navigation("Spin returned no handle and cancellation was not confirmed")
            raise NavigationInfrastructureError("Spin returned no goal handle")
        if not goal_handle.accepted:
            self._clear_active_action()
            raise NavigationInfrastructureError("Spin rejected the goal")
        try:
            result_future = goal_handle.get_result_async()
        except Exception as error:
            self.navigator.goal_handle = goal_handle
            self.navigator.result_future = None
            self._cancel_navigation("Spin did not confirm cancellation after losing its result")
            raise NavigationInfrastructureError("Spin could not request its result") from error
        self.navigator.goal_handle = goal_handle
        self.navigator.result_future = result_future
        self.navigator.status = None

    def _task_result(self) -> TaskResult | None:
        """Poll one accepted Nav2 goal without BasicNavigator's nested spin."""
        result_future = self.navigator.result_future
        if result_future is None:
            raise NavigationInfrastructureError("Nav2 lost its result future")
        self._spin_once(self.navigator, timeout_sec=0.0)
        if not result_future.done():
            return None
        try:
            result = result_future.result()
        except Exception as error:
            raise NavigationInfrastructureError("Nav2 result transport failed") from error
        if result is None:
            raise NavigationInfrastructureError("Nav2 returned no result")
        self.navigator.status = result.status
        if result.status == GoalStatus.STATUS_SUCCEEDED:
            return TaskResult.SUCCEEDED
        if result.status == GoalStatus.STATUS_ABORTED:
            return TaskResult.FAILED
        if result.status == GoalStatus.STATUS_CANCELED:
            return TaskResult.CANCELED
        return TaskResult.UNKNOWN

    def go_to_position(
        self,
        x: float,
        y: float,
        theta: float,
        local_frame: bool,
        *,
        stop_within_m: float = 0.0,
    ) -> bool:
        """Navigate to the goal, blocking until Nav2 finishes. Raises
        SkillFailed with a human-readable reason; a skill cancel unwinds as
        SkillCancelled with the Nav2 task cancelled. Returns whether a composed
        visual-navigation skill deliberately stopped short of the exact point."""
        goal_x, goal_y, goal_yaw, goal_frame = self._resolve_goal(x, y, theta, local_frame)

        goal_pose = self._pose_stamped(goal_x, goal_y, goal_yaw, goal_frame)
        path_navigator = self.navigator_mapfree if local_frame else self.navigator_navigation
        try:
            path_exists = self._path_exists(path_navigator, goal_pose)
        except NavigationPathIndeterminateError as error:
            # Humble cannot distinguish no-path from transient planner errors.
            # Preserve that classification for composed exploration while
            # attaching map evidence and a non-executed approach hint.
            raise NavigationPathIndeterminateError(
                self._no_path_detail(path_navigator, goal_x, goal_y, goal_yaw, goal_frame, local_frame)
            ) from error
        if not path_exists:
            raise SkillFailed(self._no_path_detail(path_navigator, goal_x, goal_y, goal_yaw, goal_frame, local_frame))

        # Announce only a preflight-valid goal. Publishing before getPath()
        # made the web map show a green destination that Nav2 had rejected.
        self._commanded_goal_pub.publish(goal_pose)
        self._start_navigation(goal_pose, behavior_tree="mapfree" if local_frame else "navigation")

        initial_distance = -1.0
        last_distance = -1.0
        last_recoveries = 0
        last_progress_log = 0.0
        last_pose = None
        said_close_to_goal = False
        best_distance = math.inf
        last_progress_at = time.monotonic()
        navigation_started_at = last_progress_at
        result = None
        while result is None:
            try:
                self.skill.sleep(0.1)
            except SkillCancelled:
                self._cancel_navigation("Nav2 did not acknowledge cancellation")
                raise
            now = time.monotonic()
            try:
                result = self._task_result()
            except NavigationInfrastructureError:
                self._cancel_navigation("Nav2 did not confirm cancellation after losing its result")
                raise
            if result is not None:
                break
            feedback = self.navigator.getFeedback()
            if not feedback:
                if now - last_progress_at >= NAVIGATION_STALL_TIMEOUT_S:
                    self._cancel_navigation("Nav2 did not acknowledge stalled navigation cancellation")
                    raise NavigationInfrastructureError(
                        f"Nav2 provided no progress feedback for {NAVIGATION_STALL_TIMEOUT_S:.0f}s"
                    )
                continue
            # Nav2's own distance_remaining: feedback.current_pose is in the
            # navigator's global frame while our goal may be in another.
            distance = feedback.distance_remaining
            last_recoveries = feedback.number_of_recoveries
            current = getattr(feedback, "current_pose", None)
            if current is not None:
                last_pose = (current.pose.position.x, current.pose.position.y, current.header.frame_id or "map")
            if distance > 0.0:
                last_distance = distance
                if initial_distance < 0.0:
                    initial_distance = distance
                if distance <= best_distance - NAVIGATION_PROGRESS_EPSILON_M:
                    best_distance = distance
                    last_progress_at = now
                if stop_within_m > 0.0 and distance <= stop_within_m:
                    self._cancel_navigation("Nav2 did not acknowledge visual approach cancellation")
                    self.logger.info(f"Visual approach complete with {distance:.2f}m remaining")
                    return True
                if now - last_progress_at >= NAVIGATION_STALL_TIMEOUT_S:
                    self._cancel_navigation("Nav2 did not acknowledge stalled navigation cancellation")
                    raise NavigationNoProgressError(
                        f"Nav2 made no route progress for {NAVIGATION_STALL_TIMEOUT_S:.0f}s "
                        f"with {distance:.2f}m remaining"
                    )

            if now - navigation_started_at >= NAVIGATION_MAX_DURATION_S:
                self._cancel_navigation("Nav2 did not acknowledge navigation timeout cancellation")
                raise NavigationExecutionError(
                    f"Nav2 did not finish navigation within {NAVIGATION_MAX_DURATION_S:.0f}s"
                )

            if now - last_progress_log >= 1.0:
                last_progress_log = now
                completion = 100.0 * (1.0 - distance / initial_distance) if initial_distance > 0.0 else 0.0
                self.logger.info(
                    f"Navigation progress: {max(0.0, min(100.0, completion)):.0f}% "
                    f"({distance:.2f}m remaining, {last_recoveries} recoveries)"
                )

            if stop_within_m <= 0.0 and 0.0 < distance < 0.2 and not said_close_to_goal:
                said_close_to_goal = True
                self.skill.feedback(
                    "I'm almost done with this movement, if I think I should navigate again to pursue this task"
                    ", I should stop the current primitive and start a new navigation movement."
                )

        if result == TaskResult.SUCCEEDED:
            self._clear_active_action()
            return False
        detail = f"Nav2 reported {getattr(result, 'name', result)}"
        if last_distance >= 0.0:
            detail += f" with {last_distance:.2f}m still to go"
        if last_pose is not None:
            detail += f"; the robot stopped at ({last_pose[0]:.2f}, {last_pose[1]:.2f}) in the {last_pose[2]} frame"
        if last_recoveries > 0:
            detail += f" after {last_recoveries} recovery attempt{'s' if last_recoveries != 1 else ''}"
        self._clear_active_action()
        cause = self._blocking_cause(goal_x, goal_y, goal_frame)
        if result == TaskResult.FAILED:
            detail += f"; {cause}" if cause is not None else "; the route may be blocked or the robot may be stuck"
            raise NavigationExecutionError(detail + ". The target was not reached")
        if cause is not None:
            detail += f"; {cause}"
        raise NavigationInfrastructureError(
            detail + "; the navigation action ended unexpectedly. The target was not reached"
        )

    def rotate_in_place(self, theta: float) -> None:
        """Turn with Nav2's collision-checked behavior instead of path planning."""
        if abs(theta) < math.radians(1.0):
            return
        self._start_rotation(theta)
        deadline = time.monotonic() + ROTATION_MAX_DURATION_S
        result = None
        while result is None:
            try:
                self.skill.sleep(0.1)
            except SkillCancelled:
                self._cancel_navigation("Nav2 did not acknowledge rotation cancellation")
                raise
            try:
                result = self._task_result()
            except NavigationInfrastructureError:
                self._cancel_navigation("Nav2 did not confirm rotation cancellation after losing its result")
                raise
            if time.monotonic() >= deadline and result is None:
                self._cancel_navigation("Nav2 did not acknowledge rotation timeout cancellation")
                raise NavigationExecutionError(f"Nav2 did not finish rotating within {ROTATION_MAX_DURATION_S:.0f}s")
        if result == TaskResult.SUCCEEDED:
            self._clear_active_action()
            return
        self._clear_active_action()
        if result == TaskResult.FAILED:
            raise NavigationExecutionError("Nav2 could not execute the in-place rotation")
        raise NavigationInfrastructureError(f"Nav2 reported {getattr(result, 'name', result)} while rotating in place")

    def _destroy_navigators(self) -> None:
        with self._destroy_lock:
            if self._navigators_destroyed:
                return
            self._navigators_destroyed = True
            # No spin may overlap executor shutdown or node removal. The
            # guardian reaches this point only after the exact UUID is terminal.
            with self._executor_spin_lock:
                for navigator in reversed(self._navigator_nodes):
                    try:
                        # The Humble Node.executor setter both removes the node
                        # and clears its weak reference to this executor.
                        navigator.executor = None
                    except Exception as e:
                        self.logger.warning(f"Error removing navigator from executor: {e}")
                try:
                    if not self._executor.shutdown(timeout_sec=NAVIGATION_EXECUTOR_SHUTDOWN_TIMEOUT_S):
                        self.logger.warning("Nav2 executor shutdown did not finish")
                except Exception as e:
                    self.logger.warning(f"Error shutting down Nav2 executor: {e}")
                for navigator in reversed(self._navigator_nodes):
                    try:
                        # Humble's BasicNavigator.destroy_node() misses this
                        # client; its live handle would keep graph entities alive.
                        navigator.assisted_teleop_client.destroy()
                    except Exception as e:
                        self.logger.warning(f"Error destroying assisted_teleop client: {e}")
                    try:
                        navigator.destroy_node()
                    except Exception as e:
                        self.logger.warning(f"Error destroying navigator node: {e}")

    def destroy(self):
        """Release nodes now, or hand them to an active cancellation guardian."""
        with self._guardian_lock:
            active_goal = self._active_goal_uuid is not None
            guardian = self._cancel_guardian
            guardian_alive = guardian is not None and guardian.is_alive()
            defer_to_guardian = active_goal or guardian_alive
            if defer_to_guardian:
                self._destroy_deferred = True
        if defer_to_guardian:
            if active_goal:
                self._start_cancel_guardian()
            if self._cancel_guardian_done.is_set():
                self._destroy_navigators()
            return
        self._destroy_navigators()


class NavigateToPosition(Skill):
    """Use when you need to navigate the robot to the specified position
    using provided x, y coordinates (meters), and theta_degrees (yaw) IN DEGREES.
    If local_frame is set to false, it navigates to a specific point in the map.
    If local_frame is set to true, it navigates locally, where the robot is currently (0,0)"""

    odom: Odometry
    """Resolves local_frame goals; see Nav2Controller._resolve_goal."""
    map: Map | None
    """Validates absolute goals and diagnoses keepout failures in either frame."""
    pose: Pose | None
    """Places local goals on the map for diagnosis; seeds closest-approach planning."""
    mobility: Mobility
    """Emergency zero-command fallback if Nav2 cancellation cannot be confirmed."""

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
            "Use local_frame=false only to reach absolute coordinates in the frame your pose is reported in; "
            "absolute goals are rejected unless the current occupancy map confirms known-free floor."
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

        if not local_frame:
            map_state = self.wait_for(lambda: self.map, timeout=2.0)
            if map_state is None:
                self.fail("Cannot validate an absolute goal because no current occupancy map is available")
            if error := map_goal_error(map_state, x, y):
                if error == KEEPOUT_GOAL_ERROR:
                    # Reject the target without submitting navigation, while
                    # preserving the same bounded, explicit-only approach hint
                    # reported when a planner rejects an otherwise valid goal.
                    controller = self.controller
                    error = controller._no_path_detail(
                        controller.navigator_navigation, x, y, theta, "map", False, refusal_reason=error
                    )
                self.fail(f"Refusing absolute navigation to ({x:.2f}, {y:.2f}): {error}")

        goal_desc = f"({x}, {y}, {theta_degrees} deg, {'local' if local_frame else 'map'} frame)"
        try:
            self.controller.go_to_position(x, y, theta, local_frame)
        except SkillFailed as e:
            self.fail(f"Navigation to {goal_desc} failed: {e}")
        return f"Reached position ({x}, {y}, {theta_degrees} deg)"

    def rotate_in_place(self, theta_degrees: float) -> None:
        """Internal Nav2-native rotation used by composed navigation skills."""
        self.controller.rotate_in_place(math.radians(theta_degrees))

    def navigate_relative(self, x: float, y: float, theta_degrees: float) -> None:
        """Internal collision-checked local navigation used by composed skills."""
        self.controller.go_to_position(x, y, math.radians(theta_degrees), True)

    def approach_viewpoint(
        self,
        x: float,
        y: float,
        theta_degrees: float,
        *,
        stop_within_m: float,
    ) -> bool:
        """Internal visual-exploration navigation with a safe standoff."""
        return self.controller.go_to_position(
            x,
            y,
            math.radians(theta_degrees),
            False,
            stop_within_m=stop_within_m,
        )
