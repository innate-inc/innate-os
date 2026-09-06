# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure helpers and shared types for the agent loop: no ROS, no I/O, no state."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain_client.common.enums import StrEnum
from brain_client.perception import pose as pose_math
from brain_client.perception.pose import Pose
from brain_client.state.battery import Battery


class EventKind(StrEnum):
    """What queued a stimulus — USER is load-bearing: only user speech preempts a turn."""

    INFO = "info"
    USER = "user"
    MOTION = "motion"


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


# Cold boot before NTP, on a robot whose RTC lost the time: the clock reads 1970.
_CLOCK_VALID_FROM_YEAR = 2025
_CLOCK_UNSET = "time not known"


def resolve_timezone(name: str) -> tzinfo | None:
    """An IANA zone by name, or None for the host's own — left unresolved so a
    DST change is picked up rather than frozen at boot."""
    if not name.strip():
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def clock_text(now: datetime) -> str:
    """The wall clock for the status line, or a marker the agent can act on:
    a confidently wrong time misleads it worse than a stated unknown does."""
    if now.year < _CLOCK_VALID_FROM_YEAR:
        return _CLOCK_UNSET
    return f"{now:%a %Y-%m-%d %H:%M %Z}".strip()


def observation_text(
    *,
    now: datetime,
    uptime_s: int,
    pose: Pose | None,
    battery: Battery | None,
    running_skill: str | None,
    events: list[Event],
    has_wrist_frame: bool,
) -> str:
    """The text half of a turn input: robot status and new events. Running-skill
    guidance rides the system instruction, not here — stored per turn it would
    be re-billed in every history entry (see brain/prompt.py)."""
    status = f"[{clock_text(now)} | t+{uptime_s}s]"
    if pose is not None:
        status += f" pose: x={pose[0]:.2f}m y={pose[1]:.2f}m heading={math.degrees(pose[2]):.0f}°"
    if battery is not None:
        status += f" | battery: {battery.percentage:.0%}"
    if running_skill:
        status += f" | running skill: {running_skill}"
    lines = [status]
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


def adjust_nav_goal(
    inputs: dict,
    *,
    capture_pose: Pose | None,
    current_pose: Pose | None,
    is_mapfree: bool,
    use_static_map: bool = False,
) -> dict:
    """Ground a navigate_to_position goal in the robot's current pose.

    Local goals are relative to the frame the model saw, so they first shift
    by however far the robot moved since capture. In static navigation mode
    they then become absolute endpoints so the static planner and
    occupancy-map guard own the whole route. In mapfree, mapping, and unknown
    modes they stay robot-relative. Absolute goals in mapfree mode are
    re-based onto the robot instead — the map planner would only reject them.
    """
    if inputs.get("local_frame", False):
        if use_static_map and current_pose is None:
            raise ValueError("cannot ground a local navigation goal because the current map pose is unavailable")
        adjusted = inputs
        if capture_pose is not None and current_pose is not None:
            delta = pose_math.compute_pose_delta(capture_pose, current_pose)
            adjusted = pose_math.adjust_local_nav_command(inputs, delta)
        if use_static_map:
            return pose_math.local_to_absolute_nav_command(adjusted, current_pose)
        return adjusted
    if not is_mapfree or current_pose is None:
        return inputs
    return pose_math.absolute_to_local_nav_command(inputs, current_pose)
