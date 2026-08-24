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


# An unsynced clock (no RTC battery, no network yet at boot) reads 1970, and a
# confidently wrong time misleads the agent worse than no time at all.
_CLOCK_VALID_FROM_YEAR = 2025


def resolve_timezone(name: str) -> tzinfo | None:
    """A named IANA zone, or None for the system's local zone.

    An unknown name or missing tzdata falls back to local — a misconfigured
    zone must not take the brain down with it. Local stays None rather than
    resolving to a tzinfo here: a snapshot of the host's current offset would
    freeze the brain on one side of a DST change.
    """
    if not name.strip():
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def clock_text(now: datetime) -> str:
    """The wall clock as the status line shows it, or "" when it can't be trusted.

    Minute precision: at a 3 s turn interval, seconds are noise that reads as
    something to act on. The zone abbreviation is what makes a robot left on
    UTC visible in the transcript instead of silently wrong.
    """
    if now.year < _CLOCK_VALID_FROM_YEAR:
        return ""
    return f"{now:%a %Y-%m-%d %H:%M %Z}".strip()


def observation_text(
    *,
    now: datetime,
    uptime_s: int,
    pose: Pose | None,
    battery: Battery | None,
    running_skill: str | None,
    guidance: str,
    events: list[Event],
    has_wrist_frame: bool,
) -> str:
    """The text half of a turn input: robot status, guidance, and new events."""
    clock = clock_text(now)
    status = f"[{clock} | t+{uptime_s}s]" if clock else f"[t+{uptime_s}s]"
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
