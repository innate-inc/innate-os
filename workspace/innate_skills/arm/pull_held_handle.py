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
    debug_enabled = True

    @staticmethod
    def _pose_data(pose) -> dict:
        return {"position": list(pose.position), "rpy": list(pose.rpy)}

    def _joint_data(self, effort: tuple[float, ...]) -> dict:
        state = self.joint_states
        return {
            "names": list(state.name),
            "position": list(state.position),
            "velocity": list(state.velocity),
            "effort": list(effort),
            "age_s": time.monotonic() - state.received_at,
        }

    def _stop_and_fail(self, message: str, **fields) -> None:
        """Brake before file I/O or failure unwinding on every motion fault."""
        try:
            self.manipulation.stream_stop()
        except Exception as error:
            fields["stream_stop_error"] = repr(error)
        self.debug_event("safety_stop", message=message, **fields)
        self.fail(message)

    def _effort(self, max_effort: float) -> tuple[float, ...]:
        state = self.joint_states
        age = time.monotonic() - state.received_at if state.received_at > 0.0 else None
        if age is None or age > _STATE_MAX_AGE_S:
            self._stop_and_fail("Arm effort feedback is stale", reason="stale", age_s=age, max_age_s=_STATE_MAX_AGE_S)
        values = tuple(float(value) for value in state.effort[:ARM_JOINTS])
        if len(values) != ARM_JOINTS or not all(math.isfinite(value) for value in values):
            self._stop_and_fail(
                "Arm effort feedback is incomplete", reason="incomplete", effort=list(values), age_s=age
            )
        peak = max(abs(value) for value in values)
        if peak > max_effort:
            self._stop_and_fail(
                f"Arm effort limit reached ({peak:.1f}% > {max_effort:.1f}%)",
                reason="absolute_limit",
                effort=list(values),
                peak=peak,
                limit=max_effort,
            )
        return values

    def _tare(self, max_effort: float) -> tuple[float, ...]:
        samples: list[tuple[float, ...]] = []
        for index in range(_TARE_SAMPLES):
            effort = self._effort(max_effort)
            samples.append(effort)
            self.debug_event("tare_sample", sample=index, joints=self._joint_data(effort))
            self.sleep(_TARE_PERIOD_S)
        baseline = tuple(statistics.median(sample[joint] for sample in samples) for joint in range(ARM_JOINTS))
        self.debug_event("tare_complete", baseline=list(baseline))
        return baseline

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

        self.debug_event(
            "controller_started",
            parameters={
                "distance_m": distance_m,
                "direction": list(direction),
                "max_effort_delta": max_effort_delta,
                "max_effort": max_effort,
            },
            limits={
                "state_max_age_s": _STATE_MAX_AGE_S,
                "step_m": _STEP_M,
                "step_timeout_s": _STEP_TIMEOUT_S,
                "min_progress_m": _MIN_PROGRESS_M,
                "max_stalled_steps": _MAX_STALLED_STEPS,
                "max_seconds": _MAX_SECONDS,
                "stream_max_joint_speed": _STREAM_MAX_JOINT_SPEED,
            },
            starting_pose=self._pose_data(self.manipulation.pose),
        )

        soft_delta = min(10.0, max_effort_delta * 0.45)
        guidance = PullGuidance(direction, soft_delta)
        started = time.monotonic()
        traveled = 0.0
        peak_delta = 0.0
        stalled_steps = 0

        self.logger.info(f"[pull-debug run={self.run_id or 'direct'}] taring arm effort")
        self.feedback("Taring arm effort before pull")
        baseline = self._tare(max_effort)

        try:
            step_index = 0
            while traveled < distance_m:
                if time.monotonic() - started > _MAX_SECONDS:
                    self._stop_and_fail("Contact-aware pull timed out", reason="timeout", traveled_m=traveled)

                effort = self._effort(max_effort)
                delta = load_delta(effort, baseline)
                peak_delta = max(peak_delta, delta)
                if delta > max_effort_delta:
                    self._stop_and_fail(
                        f"Contact load limit reached ({delta:.1f} > {max_effort_delta:.1f} percentage points)",
                        reason="contact_limit",
                        effort_delta=delta,
                        limit=max_effort_delta,
                    )

                heading, scale = guidance.command(delta)
                step = min(_STEP_M * scale, distance_m - traveled)
                before = self.manipulation.pose
                target = (
                    before.x + heading[0] * step,
                    before.y + heading[1] * step,
                    before.z + heading[2] * step,
                )
                roll, pitch, yaw = before.rpy
                self.debug_event(
                    "step_decision",
                    step=step_index,
                    elapsed_s=time.monotonic() - started,
                    traveled_m=traveled,
                    joints=self._joint_data(effort),
                    baseline=list(baseline),
                    effort_delta=delta,
                    heading=list(heading),
                    step_scale=scale,
                    steering_offset_rad=guidance.offset,
                    steering_turn_sign=guidance.turn_sign,
                    commanded_step_m=step,
                    before=self._pose_data(before),
                    target=list(target),
                )
                self.feedback(
                    f"Step {step_index + 1}: {traveled:.3f}/{distance_m:.3f} m, "
                    f"load +{delta:.1f}, steer {math.degrees(guidance.offset):+.0f}°"
                )
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
                    live_effort = self._effort(max_effort)
                    live_delta = load_delta(live_effort, baseline)
                    peak_delta = max(peak_delta, live_delta)
                    current = self.manipulation.pose
                    moved = math.dist(before.position, current.position)
                    self.debug_event(
                        "step_observation",
                        step=step_index,
                        elapsed_s=time.monotonic() - started,
                        step_elapsed_s=time.monotonic() - step_started,
                        joints=self._joint_data(live_effort),
                        effort_delta=live_delta,
                        current=self._pose_data(current),
                        moved_m=moved,
                    )
                    if live_delta > max_effort_delta:
                        self._stop_and_fail(
                            f"Contact load limit reached ({live_delta:.1f} > {max_effort_delta:.1f} percentage points)",
                            reason="contact_limit",
                            effort_delta=live_delta,
                            limit=max_effort_delta,
                        )
                    if moved >= step * 0.8:
                        break

                traveled += moved
                if moved < _MIN_PROGRESS_M:
                    stalled_steps += 1
                    if stalled_steps >= _MAX_STALLED_STEPS:
                        self._stop_and_fail(
                            "Handle stopped moving under the allowed effort",
                            reason="stalled",
                            stalled_steps=stalled_steps,
                            traveled_m=traveled,
                        )
                else:
                    stalled_steps = 0
                self.debug_event(
                    "step_complete",
                    step=step_index,
                    moved_m=moved,
                    traveled_m=traveled,
                    stalled_steps=stalled_steps,
                    peak_effort_delta=peak_delta,
                )
                step_index += 1
        except (ArmFailed, ArmUnhealthy) as error:
            self._stop_and_fail(f"Contact-aware pull failed: {error}", reason="arm_error")
        finally:
            # Stop the live stream on every exit, including Stop/cancellation.
            self.manipulation.stream_stop()
            self.debug_event(
                "stream_stopped",
                traveled_m=traveled,
                peak_effort_delta=peak_delta,
                pose=self._pose_data(self.manipulation.pose),
            )

        message = f"Pulled the held handle {traveled:.2f} m (peak load change {peak_delta:.1f} points)"
        self.feedback(message)
        return message
