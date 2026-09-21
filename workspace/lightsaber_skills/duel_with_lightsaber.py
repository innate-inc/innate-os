# SPDX-License-Identifier: Apache-2.0
"""Slow Astra plastic-saber sparring; preloaded grip, stationary base."""

import json
import math
import re
import time
from pathlib import Path
from types import SimpleNamespace

from innate import MainImage, Manipulation, Mobility, Skill, SkillReturn, WristImage
from innate.exceptions import ArmFailed, ArmUnhealthy, SkillFailed

from .lightsaber_policy import LightsaberPolicy

# Conservative subsets of the MARS URDF limits. No new calibration is implied.
LIMITS = ((-1.5, 1.5), (-1.5, 1.15), (-1.5, 1.67), (-1.85, 1.67), (-1.5, 1.5))


class _RecoverableArmFault(SkillFailed):
    """A stopped arm may be rebooted once before visual reassessment."""


class DuelWithLightsaber(Skill):
    """Supervised slow robot duel with an already-held 20 cm plastic saber.

    Both robots must be stationary, blades within reach in a clear fighting
    area, humans outside the swept area, and a supervisor ready to press Stop.
    Only invoke for an explicit duel request. Arm-only gentle blade contact;
    never for people, hard strikes or a fast opponent. Start with torque ON
    and the saber secured. Preserves grip on every exit. Experimental visual
    disarm assessment, not an independent referee or collision safety system.
    """

    manipulation: Manipulation
    mobility: Mobility
    main_image: MainImage
    wrist_image: WristImage
    debug_enabled = True
    starting_envelope = 0.25
    joint_limits = LIMITS
    arm_load_limit_percent = 45

    def _event(self, name, **data):
        recorder = getattr(super(), "debug_event", None)
        if recorder is not None:
            recorder(name, **data)
        else:
            self.logger.info(f"[lightsaber] {name} {json.dumps(data, allow_nan=False)}")

    def _make_policy(self):
        return LightsaberPolicy()

    @staticmethod
    def _effort_profile(motors, limits):
        """Decode the legacy arm driver's mixed load-percent/current-mA topic."""
        if len(motors) != 5 or len(limits) != 5:
            raise ValueError("Incomplete arm effort configuration")
        profile = []
        for motor, limit in zip(motors, limits, strict=True):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("Invalid arm current limit")
            if motor.startswith(("XL330-", "XC330-")):
                cap = limit or 1750  # Same x330 default as arm_config.cpp.
                if cap > 1750:
                    raise ValueError("Arm current limit exceeds x330 hardware maximum")
                profile.append((100.0 / cap, "mA", cap))
            elif motor.startswith(("XL430-", "XC430-")) and limit == 0:
                profile.append((1.0, "% load", 100))
            else:
                raise ValueError("Unsupported motor/effort configuration")
        return tuple(profile)

    @staticmethod
    def _encoding_from_source(source):
        compact = re.sub(r"\s+", "", source)
        normalized = "efforts.push_back(effortPercent(loads[j],joint_configs_[j]));" in compact
        legacy = (
            "efforts.push_back(static_cast<double>(loads[j]));" in compact
            and "efforts.push_back(static_cast<double>(loads[j])/10.0);" in compact
        )
        if normalized and not legacy:
            return "percent"
        if legacy and not normalized:
            return "legacy"
        raise ValueError("Unknown arm driver effort encoding; refusing to guess units")

    def _driver_effort_encoding(self):
        for root in Path(__file__).resolve().parents:
            source = root / "ros2_ws/src/mars_bot/mars_arm/mars_arm/arm_control.cpp"
            if source.is_file():
                return self._encoding_from_source(source.read_text())
        raise ValueError("Arm driver source unavailable; cannot establish effort units")

    def _load_effort_profile(self):
        from rcl_interfaces.srv import GetParameters

        if self._driver_effort_encoding() == "percent":
            self._effort_units = ((1.0, "% capacity", 100),) * 5
            return
        client = self.manipulation.node.create_client(GetParameters, "/mars_arm/get_parameters")
        deadline = time.monotonic() + 3
        try:
            while not client.service_is_ready():
                if time.monotonic() > deadline:
                    self.fail("Arm effort configuration service unavailable")
                self.sleep(0.04)
            request = GetParameters.Request()
            request.names = [f"joint_{i}.{field}" for i in range(1, 6) for field in ("motor_type", "current_limit")]
            future = client.call_async(request)
            while not future.done():
                if time.monotonic() > deadline:
                    self.fail("Arm effort configuration timed out")
                self.sleep(0.04)
            values = future.result().values
            if len(values) != 10 or any(v.type != (4 if i % 2 == 0 else 2) for i, v in enumerate(values)):
                self.fail("Missing arm motor/current-limit parameters")
            self._effort_units = self._effort_profile(
                [v.string_value for v in values[::2]], [v.integer_value for v in values[1::2]]
            )
        finally:
            self.manipulation.node.destroy_client(client)

    def _arm_effort_percent(self, state):
        return tuple(value * profile[0] for value, profile in zip(state.effort[:5], self._effort_units, strict=True))

    def _start_telemetry(self):
        # Subscribe to the arm-only topic: older SDK JointStates has no receive
        # timestamp and /joint_states also contains the head servo.
        from sensor_msgs.msg import JointState
        from mars_msgs.msg import ArmStatus

        self._load_effort_profile()
        self._duel_state = None
        self._duel_health = None

        def receive(msg):
            self._duel_state = SimpleNamespace(
                name=tuple(msg.name),
                position=tuple(msg.position),
                effort=tuple(msg.effort),
                received_at=time.monotonic(),
            )

        def receive_health(msg):
            self._duel_health = SimpleNamespace(is_ok=msg.is_ok, error=msg.error,
                                                is_torque_enabled=msg.is_torque_enabled,
                                                received_at=time.monotonic())

        self._health_subscription = self.manipulation.node.create_subscription(ArmStatus, "/mars/arm/status", receive_health, 1)
        self._duel_subscription = self.manipulation.node.create_subscription(JointState, "/mars/arm/state", receive, 1)
        started = time.monotonic()
        while self._duel_state is None or self._duel_health is None:
            if time.monotonic() - started > 7.0:
                self.fail("No arm telemetry or health status received")
            self.sleep(0.04)

    def _stop_telemetry(self):
        health_subscription = getattr(self, "_health_subscription", None)
        if health_subscription is not None:
            self.manipulation.node.destroy_subscription(health_subscription)
            self._health_subscription = None
        subscription = getattr(self, "_duel_subscription", None)
        if subscription is not None:
            self.manipulation.node.destroy_subscription(subscription)
            self._duel_subscription = None

    def _state(self):
        health = self._duel_health
        if health is None or not 0 <= time.monotonic() - health.received_at <= 8.0:
            self.fail("Missing or stale arm health report")
        if not health.is_ok:
            if "overload" in health.error.lower() and not any(word in health.error.lower() for word in ("temperature", "overheating", "voltage", "encoder", "electrical")):
                raise _RecoverableArmFault(f"Arm hardware fault: {health.error}")
            self.fail(f"Arm hardware fault: {health.error}")
        if not health.is_torque_enabled:
            self.fail("Arm torque must already be enabled with saber held")
        state = self._duel_state
        age = time.monotonic() - state.received_at
        if not 0 <= age <= 0.25:
            self.fail("Stale arm telemetry; duel stopped")
        expected = tuple(self.manipulation.joint_names)
        if tuple(state.name) != expected or len(state.position) != 6 or len(state.effort) != 6:
            self.fail("Missing or misordered arm telemetry")
        if not all(math.isfinite(v) for v in (*state.position, *state.effort)):
            self.fail("Non-finite arm telemetry")
        # Compare like units. Exclude the held gripper from ARM overload checks.
        for i, percent in enumerate(self._arm_effort_percent(state)):
            if self.arm_load_limit_percent is not None and abs(percent) > self.arm_load_limit_percent:
                _, unit, cap = self._effort_units[i]
                raise _RecoverableArmFault(
                    f"Arm load too high: {state.name[i]} {abs(percent):.1f}% > {self.arm_load_limit_percent:g}% "
                    f"({state.effort[i]:.1f} {unit}; configured limit {cap} {unit})"
                )
        if self.manipulation.torque_enabled is not True:
            self.fail("Arm torque must already be enabled with saber held")
        if self.joint_limits is not None and any(
            not lo <= v <= hi for v, (lo, hi) in zip(state.position[:5], self.joint_limits, strict=True)
        ):
            self.fail("Arm outside duel joint limits")
        if (
            self.starting_envelope is not None
            and hasattr(self, "_anchor")
            and max(abs(v - a) for v, a in zip(state.position[:5], self._anchor, strict=True)) > self.starting_envelope + 0.01
        ):
            self.fail("Arm left the starting duel envelope")
        return state

    def _wait(self, seconds):
        self._recovery_wait(seconds)
        self._state()  # Also checks effort while waiting for GPT.

    def _recovery_wait(self, seconds):
        self.sleep(seconds)
        self.check_cancelled()
        for attr, (previous, stamp) in getattr(self, "_camera_seen", {}).copy().items():
            frame = getattr(self, attr)
            if frame is None:
                self.fail("Camera feed missing during duel")
            if frame is not previous:
                self._camera_seen[attr] = (frame, time.monotonic())
            elif time.monotonic() - stamp > 1.5:
                self.fail("Camera feed stalled during duel")
        if time.monotonic() > self._deadline:
            self.fail("Duel time budget exhausted")

    def _observe(self, step, *, recovery=False):
        wait = self._recovery_wait if recovery else self._wait
        frames = {}
        for name, attr in (("head", "main_image"), ("wrist", "wrist_image")):
            previous = getattr(self, attr)
            start = time.monotonic()
            while getattr(self, attr) is previous:
                if time.monotonic() - start > 1.5:
                    self.fail(f"No fresh {name} camera frames")
                wait(0.04)
            frame = getattr(self, attr)
            if frame is None:
                self.fail(f"Missing {name} camera frame")
            frames[name] = frame
        state = self._duel_state if recovery else self._state()
        if recovery and (time.monotonic() - state.received_at > 0.25 or len(state.position) != 6 or
                         not all(math.isfinite(v) for v in state.position)):
            self.fail("Recovery requires fresh finite arm telemetry")
        observation = {
            "step": step,
            "recovery_grip_check": recovery,
            "joint_names": list(state.name),
            "joint_positions_rad": list(state.position),
            "joint_efforts_driver_units": list(state.effort),
            "arm_efforts_percent": list(self._arm_effort_percent(state)),
            "starting_joints_rad": list(self._anchor),
            "saber_length_m": 0.20,
            "base_stationary": True,
        }
        self._event("lightsaber_observation", **observation)
        return observation, frames

    def _move(self, values, observed):
        current = self._state().position[:5]
        if max(abs(v - old) for v, old in zip(current, observed[:5], strict=True)) > 0.015:
            raise ValueError("Arm moved during model decision; discard this action and reassess fresh images")
        index, delta, _ = values
        target = list(current)
        target[int(index) - 1] += delta
        if self.joint_limits is not None and any(
            not lo <= v <= hi for v, (lo, hi) in zip(target, self.joint_limits, strict=True)
        ):
            raise ValueError("Requested joint target outside duel limits")
        if self.starting_envelope is not None and max(
            abs(v - a) for v, a in zip(target, self._anchor, strict=True)
        ) > self.starting_envelope:
            raise ValueError(f"Requested target exceeds {self.starting_envelope} rad starting envelope")
        started = time.monotonic()
        self._event("lightsaber_move_start", target=target, start=list(current))
        try:
            while True:
                self.check_cancelled()
                state = self._state()
                if max(abs(v - t) for v, t in zip(state.position[:5], target, strict=True)) <= min(0.015, abs(delta) / 3):
                    return {"complete": True, "measured_delta_rad": state.position[int(index) - 1] - current[int(index) - 1]}
                if time.monotonic() - started > 2.0:
                    errors = [round(v - t, 5) for v, t in zip(state.position[:5], target, strict=True)]
                    self._event("lightsaber_move_timeout", target=target, measured=list(state.position[:5]), errors=errors)
                    moved = state.position[int(index) - 1] - current[int(index) - 1]
                    other_error = max(abs(e) for i, e in enumerate(errors) if i != int(index) - 1)
                    if moved * delta > 0 and 0.25 <= moved / delta < 1 and abs(moved) >= 0.01 and other_error <= 0.015:
                        return {"complete": False, "measured_delta_rad": moved, "remaining_rad": delta - moved}
                    raise _RecoverableArmFault(f"Arm did not track duel motion; joint errors rad={errors}")
                # Streaming is stoppable; discrete goto motions cannot be preempted.
                # Five joints deliberately preserve the existing standing grip target.
                self.manipulation.stream_joints(target, max_speed=0.25)
                self._wait(0.04)
        finally:
            self.manipulation.stream_stop()

    def _recover_arm(self, policy, reason, step):
        self.manipulation.stream_stop()
        self.mobility.stop()
        if self._recovery_used or getattr(self, "_suspected_win", False):
            self.fail(f"Arm recovery unavailable; stopping: {reason}")
        grip = getattr(self.manipulation, "_grip_target", None)
        if grip is None or not math.isfinite(grip):
            self.fail("Cannot reboot without a known standing grip target")
        self._recovery_used = True
        self._event("lightsaber_recovery_start", reason=reason)
        self.feedback("Arm fault: stopping and rebooting once; fresh grip check before resuming")
        self.check_cancelled()
        if not self.manipulation.reboot_servos():
            self.fail("Automatic arm reboot failed")
        self.check_cancelled()  # A Stop during the reboot must prevent torque restoration.
        rebooted = time.monotonic()
        while self._duel_health is None or self._duel_health.received_at <= rebooted:
            if time.monotonic() - rebooted > 7:
                self.fail("No fresh arm health report after reboot")
            self._recovery_wait(0.04)
        if not self._duel_health.is_ok:
            self.fail(f"Arm still unhealthy after reboot: {self._duel_health.error}")
        observation, frames = self._observe(step, recovery=True)
        call, (action, _, scene, note) = policy.decide(observation, frames, self._recovery_wait)
        policy.result(call, "Recovery grip assessment only; no model motion dispatched.")
        if action != "observe" or scene != "ready":
            self.fail(f"Recovery stopped ({scene}): {note}")
        self.check_cancelled()
        if not self.manipulation.torque_on():
            self.fail("Failed to restore torque after automatic reboot")
        self.manipulation._grip_target = grip  # Preserve the pre-fault setting, never increase it.
        self.check_cancelled()
        enabled = time.monotonic()
        while self._duel_health.received_at <= enabled or not self._duel_health.is_torque_enabled:
            if time.monotonic() - enabled > 7:
                self.fail("Torque restoration was not confirmed")
            self._recovery_wait(0.04)
        state = self._state()
        # Resend only the measured arm pose and the original standing grip target.
        try:
            self.manipulation.stream_joints(state.position[:5], max_speed=0.25)
            self._wait(0.08)
        finally:
            self.manipulation.stream_stop()
        policy.history.append({"role": "user", "content": [{"type": "input_text", "text":
            "Arm recovered once. Previous motion was interrupted, not completed. Use fresh images and joints; choose a different approach rather than force the failed direction. No further reboot is available."}]})
        self._event("lightsaber_recovery_complete", reason=reason)

    def execute(self, max_steps: int = 30) -> SkillReturn:
        """Attempt a supervised disarm in 1–60 decisions and at most 120 s."""
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 60:
            self.fail("max_steps must be an integer between 1 and 60")
        policy = self._make_policy()  # Missing credentials fail before commands.
        self._deadline = time.monotonic() + 120
        if hasattr(self, "_anchor"):
            del self._anchor
        self._camera_seen = {attr: (getattr(self, attr), time.monotonic()) for attr in ("main_image", "wrist_image")}
        won = 0
        self._recovery_used = False
        self._suspected_win = False
        rejected = 0
        partial_counts = {}
        try:
            self._start_telemetry()
            self._anchor = tuple(self._state().position[:5])
            self.mobility.stop()
            for step in range(max_steps):
                call = None
                try:
                    observation, frames = self._observe(step)
                    try:
                        call, (action, values, scene, note) = policy.decide(observation, frames, self._wait)
                    finally:
                        if policy.last_usage is not None:
                            self._event("gpt_usage", step=step, **policy.last_usage)
                    self.check_cancelled()
                    self._state()
                    self._event("lightsaber_action", action=action, values=values, scene=scene, note=note)
                    self.feedback(f"Astra duel {step + 1}: {action} — {note}")
                    if scene in ("lost", "unsafe") or action == "give_up":
                        self.fail(f"Duel stopped ({scene}): {note}")
                    if scene == "won":
                        self._suspected_win = True
                        won += 1
                        if won >= 2:
                            return f"Astra visually reports opponent disarmed in two observations; own saber held: {note}"
                        policy.result(call, "Keep still. Obtain a second fresh observation to verify the disarm.")
                        continue
                    if won:
                        self.fail("Suspected disarm not confirmed; stop for supervisor review")
                    movement = None
                    try:
                        if action == "joint_step":
                            movement = self._move(values, observation["joint_positions_rad"])
                            joint = int(values[0])
                            partial_counts[joint] = 0 if movement["complete"] else partial_counts.get(joint, 0) + 1
                            if partial_counts[joint] >= 3:
                                raise _RecoverableArmFault(f"Joint {joint} repeatedly stopped short")
                    except ValueError as error:
                        rejected += 1
                        policy.result(call, f"Rejected before motion: {error}")
                        if rejected >= 3:
                            self.fail("Three rejected duel actions")
                        continue
                    rejected = 0
                    if movement is not None and not movement["complete"]:
                        policy.result(call, "Streaming stopped after partial movement: " + json.dumps(movement) +
                                      ". Target was NOT reached. Reassess fresh images and measured joints; do not force a blocked direction.")
                    else:
                        policy.result(call, "Action completed; assess its effect using the next fresh observation.")
                except _RecoverableArmFault as error:
                    if call is not None:
                        policy.result(call, f"Motion interrupted by arm fault: {error}. No target success is claimed.")
                    self._recover_arm(policy, str(error), step)
                    rejected = 0
                    partial_counts.clear()
            self.fail("Duel decision budget exhausted; disarm not verified")
        except (ArmFailed, ArmUnhealthy, ValueError, RuntimeError, OSError) as error:
            self.fail(str(error))
        finally:
            self.manipulation.stream_stop()
            self.mobility.stop()
            self._stop_telemetry()
