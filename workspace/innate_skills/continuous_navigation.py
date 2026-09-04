# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Standalone continuous visual navigation.

Unlike the local brain's built-in continuous-navigation mode, this is a real
long-running skill. It owns its camera/model loop and keeps reassessing while
its Nav2 action is still moving. A new visual waypoint replaces the in-flight
goal; KEEP_CURRENT leaves Nav2 alone so the base does not brake and accelerate
again on every model turn.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from innate_skills.navigate_to_position import Nav2Controller

from brain_client.perception.pose import compute_pose_delta
from innate import HeadState, MainImage, Map, Odometry, Pose, Skill, SkillReturn, resource
from innate import gemini as gemlib
from innate.geometry import IMG_H, IMG_W, pixel_to_floor

REASSESS_INTERVAL_S = 3.0
MAX_VISUAL_RANGE_M = 3.5
STANDOFF_M = 0.35
SAME_GOAL_DISTANCE_M = 0.45
SAME_GOAL_HEADING_RAD = math.radians(25.0)
MAX_CONSECUTIVE_MODEL_FAILURES = 3


class NavigationStatus(str, Enum):
    CONTINUE = "CONTINUE"
    KEEP_CURRENT = "KEEP_CURRENT"
    OBJECTIVE_REACHED = "OBJECTIVE_REACHED"
    CANNOT_PROCEED = "CANNOT_PROCEED"


@dataclass(frozen=True)
class VisualDecision:
    status: NavigationStatus
    explanation: str
    progress_description: str = ""
    x: int | None = None
    y: int | None = None


@dataclass(frozen=True)
class NavigationDirective:
    decision: VisualDecision
    # Stable odom-frame destination. The model may take seconds to answer and
    # Nav2 may need time to cancel, so retaining robot-relative coordinates
    # here would make every replacement waypoint stale before it starts.
    odom_goal: tuple[float, float, float] | None = None


def parse_visual_decision(reply: str | None, *, allow_keep: bool) -> VisualDecision | None:
    """Parse the model's bounded JSON contract; malformed output is no action."""
    if not reply:
        return None
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(reply[start : end + 1])
        status = NavigationStatus(str(payload.get("status", "")).strip().upper())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

    explanation = str(payload.get("explanation") or "").strip()[:500]
    progress = str(payload.get("progress_description") or "").strip()[:500]
    if not explanation:
        return None
    if status == NavigationStatus.KEEP_CURRENT:
        return VisualDecision(status, explanation, progress) if allow_keep else None
    if status != NavigationStatus.CONTINUE:
        return VisualDecision(status, explanation, progress)

    x, y = payload.get("x"), payload.get("y")
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    x, y = round(x), round(y)
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        return None
    return VisualDecision(status, explanation, progress, x=x, y=y)


def decision_odom_goal(
    decision: VisualDecision,
    frame: MainImage,
    head_pitch_degrees: float,
    capture_pose: tuple[float, float, float],
) -> tuple[float, float, float] | None:
    """Ground a model-selected floor pixel into a stable odom-frame goal."""
    if decision.x is None or decision.y is None:
        return None
    u = decision.x / 1000.0 * (IMG_W - 1)
    v = decision.y / 1000.0 * (IMG_H - 1)
    floor = pixel_to_floor(u, v, head_pitch_degrees)
    if floor is None:
        return None

    floor_x, floor_y = floor
    distance = math.hypot(floor_x, floor_y)
    if distance > MAX_VISUAL_RANGE_M:
        scale = MAX_VISUAL_RANGE_M / distance
        floor_x, floor_y, distance = floor_x * scale, floor_y * scale, MAX_VISUAL_RANGE_M
    travel = max(0.0, distance - STANDOFF_M)
    heading = math.atan2(floor_y, floor_x)
    local_x = travel * math.cos(heading)
    local_y = travel * math.sin(heading)

    ox, oy, oth = capture_pose
    c, s = math.cos(oth), math.sin(oth)
    return (
        ox + c * local_x - s * local_y,
        oy + s * local_x + c * local_y,
        math.atan2(math.sin(oth + heading), math.cos(oth + heading)),
    )


def local_goal(
    odom_goal: tuple[float, float, float], current_pose: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Re-express a stable odom destination in the robot's current frame."""
    return compute_pose_delta(current_pose, odom_goal)


def _same_goal(a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    heading_delta = abs(math.atan2(math.sin(a[2] - b[2]), math.cos(a[2] - b[2])))
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= SAME_GOAL_DISTANCE_M and heading_delta <= SAME_GOAL_HEADING_RAD


class ContinuousNavigation(Skill):
    """Continuously navigate toward a distant visually described target.

    This is the separately launchable skill implementation. It uses fresh
    head-camera frames while Nav2 is moving, keeps the current waypoint when
    it remains appropriate, replaces it when the visual route changes, and
    stops only when the target is visibly reached or progress is unsafe.
    Requires target_description (for example, "the traffic lights at the
    intersection"). Use the local agent's nav_insight_continuous mode instead
    when you want the agent itself to retain control of the loop.
    """

    main_image: MainImage
    head_position: HeadState
    odom: Odometry
    map: Map | None
    pose: Pose | None

    @resource
    def _proxy(self):
        return gemlib.make_client()

    @resource
    def controller(self) -> Iterator[Nav2Controller]:
        controller = Nav2Controller(self)
        yield controller
        controller.destroy()

    def execute(self, target_description: str, max_iterations: int = 50) -> SkillReturn:
        target = target_description.strip()
        if not target:
            self.fail("target_description cannot be empty")
        if self._proxy is None:
            self.fail("Continuous navigation needs Innate Gemini access; check INNATE_SERVICE_KEY")

        self._target = target[:500]
        self._max_iterations = max(1, min(int(max_iterations), 100))
        self._iteration = 0
        self._previous_image: MainImage | None = None
        self._next_assessment_at = 0.0
        self._consecutive_model_failures = 0
        self._active_goal: tuple[float, float, float] | None = None
        self.on_cancel(self.controller.cancel_navigation)

        self.feedback(f"Starting continuous visual navigation toward: {self._target}")
        directive = self._decide(is_moving=False)
        while True:
            if directive.decision.status == NavigationStatus.OBJECTIVE_REACHED:
                return f"Reached {self._target}: {directive.decision.explanation}"
            if directive.decision.status == NavigationStatus.CANNOT_PROCEED:
                self.fail(f"Could not reach {self._target}: {directive.decision.explanation}")
            if directive.odom_goal is None:
                self.fail("Continuous navigation did not produce a grounded waypoint")

            self._active_goal = directive.odom_goal
            current = self.odom
            x, y, theta = local_goal(self._active_goal, (current.x, current.y, current.theta))
            self._next_assessment_at = time.monotonic() + REASSESS_INTERVAL_S
            replanned = self.controller.go_to_position(x, y, theta, True, reassess=self._reassess_while_moving)
            if isinstance(replanned, NavigationDirective):
                directive = replanned
                continue

            # A waypoint is only progress, never proof that the semantic target
            # was reached. Look again and let vision make that determination.
            self.feedback("Waypoint reached; reassessing the objective from the current view")
            directive = self._decide(is_moving=False)

    def _reassess_while_moving(self) -> NavigationDirective | None:
        if time.monotonic() < self._next_assessment_at:
            return None
        self._next_assessment_at = time.monotonic() + REASSESS_INTERVAL_S
        directive = self._decide(is_moving=True)
        if directive.decision.status == NavigationStatus.KEEP_CURRENT:
            return None
        if (
            directive.odom_goal is not None
            and self._active_goal is not None
            and _same_goal(directive.odom_goal, self._active_goal)
        ):
            self.feedback("Visual route is unchanged; keeping the current Nav2 waypoint")
            return None
        return directive

    def _decide(self, *, is_moving: bool) -> NavigationDirective:
        if self._iteration >= self._max_iterations:
            return NavigationDirective(
                VisualDecision(
                    NavigationStatus.CANNOT_PROCEED,
                    f"maximum of {self._max_iterations} visual reassessments reached",
                )
            )

        frame = self.main_image
        head = self.head_position
        odom = self.odom
        capture_pose = (odom.x, odom.y, odom.theta)
        previous = self._previous_image
        self._iteration += 1
        prompt = self._prompt(is_moving=is_moving, has_previous=previous is not None)
        images = [frame, previous] if previous is not None else frame
        reply = gemlib.ask_image(self._proxy, images, prompt, logger=self.logger, retries=1 if is_moving else 2)
        self.check_cancelled()
        decision = parse_visual_decision(reply, allow_keep=is_moving)
        self._previous_image = frame

        if decision is None:
            self._consecutive_model_failures += 1
            if is_moving and self._consecutive_model_failures < MAX_CONSECUTIVE_MODEL_FAILURES:
                self.feedback("Visual reassessment was invalid; keeping the current safe waypoint")
                return NavigationDirective(
                    VisualDecision(NavigationStatus.KEEP_CURRENT, "invalid visual response; keeping current waypoint")
                )
            return NavigationDirective(
                VisualDecision(
                    NavigationStatus.CANNOT_PROCEED, "visual navigation model did not return a valid decision"
                )
            )

        self._consecutive_model_failures = 0
        detail = decision.progress_description or decision.explanation
        self.feedback(f"[{self._iteration}/{self._max_iterations}] {detail}")
        if decision.status != NavigationStatus.CONTINUE:
            return NavigationDirective(decision)

        goal = decision_odom_goal(decision, frame, head.pitch_degrees, capture_pose)
        if goal is None:
            if is_moving:
                self.feedback("Selected point was not on reachable floor; keeping the current waypoint")
                return NavigationDirective(
                    VisualDecision(NavigationStatus.KEEP_CURRENT, "selected point did not project onto the floor")
                )
            return NavigationDirective(
                VisualDecision(NavigationStatus.CANNOT_PROCEED, "the selected point did not project onto the floor")
            )
        return NavigationDirective(decision, goal)

    def _prompt(self, *, is_moving: bool, has_previous: bool) -> str:
        moving = (
            "The robot is CURRENTLY MOVING toward its previous safe waypoint. Return KEEP_CURRENT if that waypoint "
            "still follows the best visible route; use CONTINUE only if it should be replaced."
            if is_moving
            else "The robot is stopped and needs a new waypoint; do not return KEEP_CURRENT."
        )
        previous = "Image 2 is the previous assessed view for progress comparison." if has_previous else ""
        return f"""You are the visual navigation controller for a wheeled indoor robot.

OBJECTIVE: {self._target}
ASSESSMENT: {self._iteration}/{self._max_iterations}
Image 1 is the fresh current head-camera view. {previous}
{moving}

Choose exactly one status:
- CONTINUE: provide x and y from 0 to 1000 for a visibly clear point ON THE FLOOR in Image 1 that advances the objective.
- KEEP_CURRENT: only while already moving and the existing waypoint remains appropriate.
- OBJECTIVE_REACHED: only with current visual evidence that the robot has reached a useful stopping position.
- CANNOT_PROCEED: the route is visibly blocked, unsafe, or the objective cannot be located after reasonable exploration.

Do not point through objects, walls, vehicles, curbs, or people. For a visible target, point at clear floor leading toward
it, not on the object itself. Return JSON only:
{{"status":"CONTINUE|KEEP_CURRENT|OBJECTIVE_REACHED|CANNOT_PROCEED","x":0,"y":0,
"explanation":"brief reason","progress_description":"change since the previous view"}}
"""
