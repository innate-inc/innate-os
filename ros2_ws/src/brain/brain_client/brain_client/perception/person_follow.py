# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure person-following control law."""

from __future__ import annotations

import math
from dataclasses import dataclass

from brain_client.common.enums import StrEnum
from brain_client.perception.face_lock import FaceBox

BODY_HEIGHT_DEADBAND = 0.12
HORIZONTAL_DEADBAND = 0.06
MAX_FORWARD_STEP_M = 0.35
MIN_FORWARD_STEP_M = 0.15
FORWARD_GAIN_M = 0.8
CAMERA_HFOV_RAD = math.radians(100.0)
TURN_IN_PLACE_THRESHOLD_RAD = math.radians(15.0)


class FollowState(StrEnum):
    IDLE = "idle"
    TRACKING = "tracking"
    LOST = "lost"


class FollowStartResult(StrEnum):
    STARTED = "started"
    ALREADY_FOLLOWING = "already_following"
    NO_LOCK = "no_lock"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True)
class FollowTarget:
    forward_m: float
    bearing_rad: float
    reference_height: float
    observed_height: float

    @property
    def needs_goal(self) -> bool:
        return self.forward_m > 0.0 or self.bearing_rad != 0.0


class PersonFollowController:
    """Keep one locked person's apparent body height and horizontal bearing."""

    def __init__(self) -> None:
        self._state = FollowState.IDLE
        self._reference_height = 0.0
        self._observed_height = 0.0

    @property
    def state(self) -> FollowState:
        return self._state

    @property
    def reference_height(self) -> float:
        return self._reference_height

    @property
    def observed_height(self) -> float:
        return self._observed_height

    @property
    def is_following(self) -> bool:
        return self._state is FollowState.TRACKING

    def start(self, body: FaceBox) -> None:
        if body.height <= 0.0:
            raise ValueError("locked person has no measurable body height")
        self._reference_height = body.height
        self._observed_height = body.height
        self._state = FollowState.TRACKING

    def stop(self) -> None:
        self._state = FollowState.IDLE
        self._reference_height = 0.0
        self._observed_height = 0.0

    def observe(self, body: FaceBox | None) -> FollowTarget | None:
        if not self.is_following:
            return None
        if body is None:
            self._state = FollowState.LOST
            return None

        self._observed_height = body.height
        size_error = 1.0 - body.height / self._reference_height
        forward_m = 0.0
        if size_error > BODY_HEIGHT_DEADBAND:
            beyond_deadband = size_error - BODY_HEIGHT_DEADBAND
            forward_m = min(MAX_FORWARD_STEP_M, max(MIN_FORWARD_STEP_M, FORWARD_GAIN_M * beyond_deadband))

        horizontal_error = 0.5 - body.center_x
        bearing_rad = 0.0 if abs(horizontal_error) <= HORIZONTAL_DEADBAND else horizontal_error * CAMERA_HFOV_RAD
        if abs(bearing_rad) > TURN_IN_PLACE_THRESHOLD_RAD:
            forward_m = 0.0
        return FollowTarget(
            forward_m=forward_m,
            bearing_rad=bearing_rad,
            reference_height=self._reference_height,
            observed_height=self._observed_height,
        )
