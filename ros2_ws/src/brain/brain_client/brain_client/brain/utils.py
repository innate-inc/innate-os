# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure helpers and shared types for the agent loop: no ROS, no I/O, no state."""

from __future__ import annotations

import math
from dataclasses import dataclass

from brain_client.common.enums import StrEnum
from brain_client.perception import pose as pose_math
from brain_client.perception.pose import Pose
from brain_client.state.battery import Battery


class EventKind(StrEnum):
    """What queued a stimulus — USER is load-bearing: only user speech preempts a turn."""

    INFO = "info"
    USER = "user"
    MOTION = "motion"
    PERSON = "person"


@dataclass(frozen=True)
class Event:
    """One queued stimulus: user speech, a skill result, sensor input."""

    text: str
    image: bytes | None = None
    kind: EventKind = EventKind.INFO


class FrameLabel(StrEnum):
    """Where a turn image came from. WRIST frames are latest-only in history —
    a stale gripper close-up reads as current grasp state."""

    HEAD = "head camera"
    WRIST = "wrist camera"
    EVENT = "event image"


Frame = tuple[FrameLabel, bytes]
"""A turn image as (label, jpeg) — only the bytes go to the model; the labels
feed telemetry and mark which frames are latest-only in history."""


class TraceEvent(StrEnum):
    """Telemetry event names on /brain/trace — the monitor keys off these values."""

    EVENT = "event"
    TURN_START = "turn_start"
    TURN_REQUEST = "turn_request"
    TURN_END = "turn_end"
    TURN_ERROR = "turn_error"
    TURN_DROPPED = "turn_dropped"
    TURN_PREEMPTED = "turn_preempted"
    SNAPSHOT = "snapshot"


def observation_text(
    *,
    uptime_s: int,
    pose: Pose | None,
    battery: Battery | None,
    running_skill: str | None,
    guidance: str,
    events: list[Event],
    has_wrist_frame: bool,
) -> str:
    """The text half of a turn input: robot status, guidance, and new events."""
    status = f"[t+{uptime_s}s]"
    if pose is not None:
        status += f" pose: x={pose[0]:.2f}m y={pose[1]:.2f}m heading={math.degrees(pose[2]):.0f}°"
    if battery is not None:
        status += f" | battery: {battery.percentage:.0%}"
    if running_skill:
        status += f" | running skill: {running_skill}"
    lines = [status]
    if guidance:
        lines.append(f"(guidance while this skill runs: {guidance})")
    lines += [f"- {event.text}" for event in events]
    if has_wrist_frame:
        lines.append("(second image is the arm wrist camera)")
    return "\n".join(lines)


def parse_view_point(args: dict) -> tuple[float, float] | None:
    """(y, x) from a go_to_point_in_view call, or None when malformed."""
    try:
        return float(args["y"]), float(args["x"])
    except (KeyError, TypeError, ValueError):
        return None


def in_image(y: float, x: float) -> bool:
    """Whether a pointed-at (y, x) lies within the 0-1000 image coordinates.

    Out-of-range values still produce finite tan() rays that ground a
    plausible-looking but wrong goal — reject them so the model re-points.
    """
    return 0.0 <= y <= 1000.0 and 0.0 <= x <= 1000.0


def adjust_nav_goal(inputs: dict, *, capture_pose: Pose | None, current_pose: Pose | None, is_mapfree: bool) -> dict:
    """Ground a navigate_to_position goal in the robot's current pose.

    Local goals are relative to the frame the model saw, so they shift by
    however far the robot moved since capture. Absolute goals only exist on a
    map; in mapfree mode they are re-based onto the robot instead — the map
    planner would only reject them.
    """
    if inputs.get("local_frame", False):
        if capture_pose is None or current_pose is None:
            return inputs
        delta = pose_math.compute_pose_delta(capture_pose, current_pose)
        return pose_math.adjust_local_nav_command(inputs, delta)
    if not is_mapfree or current_pose is None:
        return inputs
    return pose_math.absolute_to_local_nav_command(inputs, current_pose)
