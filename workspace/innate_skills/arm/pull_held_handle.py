# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Contact-aware pulling for a handle the gripper already holds."""

import math
import statistics
import time

from innate import JointStates, Manipulation, Skill, SkillReturn
from innate.exceptions import ArmFailed, ArmUnhealthy

from .pull_control import ARM_JOINTS, PullGuidance, load_delta, normalize

_STATE_MAX_AGE_S = 0.25
_TARE_SAMPLES = 10
_TARE_PERIOD_S = 0.04
_CONTROL_PERIOD_S = 0.02
_STEP_M = 0.006
_STEP_TIMEOUT_S = 0.45
_MIN_PROGRESS_M = 0.001
_MAX_STALLED_STEPS = 3
_MAX_SECONDS = 30.0
_STREAM_MAX_JOINT_SPEED = 0.35


class PullHeldHandle(Skill):
    """Pull a door, drawer, or cabinet handle that is already held by the
    gripper. The starting pull direction is expressed in base_link; toward
    the robot is usually x=-1. The skill tares gravity/load at the starting
    pose, moves in tiny Cartesian increments, slows and steers when resistance
    rises, and stops on stale feedback, excessive effort, lack of progress,
    timeout, cancellation, or the requested travel distance.

    This is an experimental contact skill. Keep the first trials short and
    supervise the robot with an unobstructed emergency-stop path.
    """

    manipulation: Manipulation
    joint_states: JointStates

    def _effort(self, max_effort: float) -> tuple[float, ...]:
        state = self.joint_states
        if state.received_at <= 0.0 or time.monotonic() - state.received_at > _STATE_MAX_AGE_S:
            self.fail("Arm effort feedback is stale")
        values = tuple(float(value) for value in state.effort[:ARM_JOINTS])
        if len(values) != ARM_JOINTS or not all(math.isfinite(value) for value in values):
            self.fail("Arm effort feedback is incomplete")
        peak = max(abs(value) for value in values)
        if peak > max_effort:
            self.fail(f"Arm effort limit reached ({peak:.1f}% > {max_effort:.1f}%)")
        return values

    def _tare(self, max_effort: float) -> tuple[float, ...]:
        samples: list[tuple[float, ...]] = []
        for _ in range(_TARE_SAMPLES):
            samples.append(self._effort(max_effort))
            self.sleep(_TARE_PERIOD_S)
        return tuple(statistics.median(sample[joint] for sample in samples) for joint in range(ARM_JOINTS))

    def execute(
        self,
        distance_m: float = 0.20,
        direction_x: float = -1.0,
        direction_y: float = 0.0,
        direction_z: float = 0.0,
        max_effort_delta: float = 25.0,
        max_effort: float = 90.0,
    ) -> SkillReturn:
        if not 0.01 <= distance_m <= 0.40:
            self.fail("distance_m must be between 0.01 and 0.40")
        if not 5.0 <= max_effort_delta <= 50.0:
            self.fail("max_effort_delta must be between 5 and 50 percentage points")
        if not 20.0 <= max_effort <= 100.0:
            self.fail("max_effort must be between 20 and 100 percent")
        try:
            direction = normalize(direction_x, direction_y, direction_z)
        except ValueError as error:
            self.fail(str(error))

        soft_delta = min(10.0, max_effort_delta * 0.45)
        guidance = PullGuidance(direction, soft_delta)
        started = time.monotonic()
        traveled = 0.0
        peak_delta = 0.0
        stalled_steps = 0

        self.logger.info("Taring arm effort before contact-aware pull")
        baseline = self._tare(max_effort)

        try:
            while traveled < distance_m:
                if time.monotonic() - started > _MAX_SECONDS:
                    self.fail("Contact-aware pull timed out")

                effort = self._effort(max_effort)
                delta = load_delta(effort, baseline)
                peak_delta = max(peak_delta, delta)
                if delta > max_effort_delta:
                    self.fail(f"Contact load limit reached ({delta:.1f} > {max_effort_delta:.1f} percentage points)")

                heading, scale = guidance.command(delta)
                step = min(_STEP_M * scale, distance_m - traveled)
                before = self.manipulation.pose
                target = (
                    before.x + heading[0] * step,
                    before.y + heading[1] * step,
                    before.z + heading[2] * step,
                )
                roll, pitch, yaw = before.rpy
                self.manipulation.stream_to(
                    *target,
                    roll=roll,
                    pitch=pitch,
                    yaw=yaw,
                    max_speed=_STREAM_MAX_JOINT_SPEED,
                )

                step_started = time.monotonic()
                moved = 0.0
                while time.monotonic() - step_started < _STEP_TIMEOUT_S:
                    self.sleep(_CONTROL_PERIOD_S)
                    live_delta = load_delta(self._effort(max_effort), baseline)
                    peak_delta = max(peak_delta, live_delta)
                    if live_delta > max_effort_delta:
                        self.fail(
                            f"Contact load limit reached ({live_delta:.1f} > {max_effort_delta:.1f} percentage points)"
                        )
                    current = self.manipulation.pose
                    moved = math.dist(before.position, current.position)
                    if moved >= step * 0.8:
                        break

                traveled += moved
                if moved < _MIN_PROGRESS_M:
                    stalled_steps += 1
                    if stalled_steps >= _MAX_STALLED_STEPS:
                        self.fail("Handle stopped moving under the allowed effort")
                else:
                    stalled_steps = 0
        except (ArmFailed, ArmUnhealthy) as error:
            self.fail(f"Contact-aware pull failed: {error}")
        finally:
            # Stop the live stream on every exit, including Stop/cancellation.
            self.manipulation.stream_stop()

        return f"Pulled the held handle {traveled:.2f} m (peak load change {peak_delta:.1f} points)"
