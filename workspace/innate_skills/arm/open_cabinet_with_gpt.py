# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Experimental camera/telemetry tool agent for the house cabinet fixture."""

import math
import time

from innate import (
    Head,
    JointStates,
    MainImage,
    Manipulation,
    Mobility,
    Odometry,
    Skill,
    SkillReturn,
    WristImage,
    resource,
)
from innate.exceptions import ArmFailed, ArmUnhealthy

from .cabinet_agent_policy import CabinetPolicy


class OpenCabinetWithGpt(Skill):
    """Open the lower kitchen cabinet with GPT-6 Astra and camera feedback.

    Start facing the cabinet within about 60 cm, arm clear of obstacles and
    gripper empty. Experimental: developed for the house simulator fixture.
    Uses the Innate service key, or OPENAI_API_KEY for local development.
    Uses head/wrist images and measured poses, bounded level-arm/base actions,
    and visual verification.
    No scene-open command or privileged cabinet pose is exposed to the model.
    """

    manipulation: Manipulation
    mobility: Mobility
    head: Head
    main_image: MainImage
    wrist_image: WristImage
    joint_states: JointStates
    odom: Odometry
    debug_enabled = True
    _frame_index = 0

    def _make_policy(self):
        return CabinetPolicy()

    @resource
    def _level_ik(self):
        from .level_handle_ik import LevelHandleIK

        # The frontal cabinet approach must clear the base. The sim applies
        # a -0.25 rad shoulder floor beyond the URDF limits (hardware: -0.5).
        # Keep this conservative floor for every pose, including side reaches.
        return LevelHandleIK(shoulder_floor=-0.25)

    def _effort(self):
        age = time.monotonic() - self.joint_states.received_at
        if self.joint_states.received_at <= 0.0 or age > 0.25:
            self.fail("Arm effort feedback is stale during handle approach")
        values = tuple(float(value) for value in self.joint_states.effort[:5])
        if len(values) != 5 or not all(math.isfinite(value) for value in values):
            self.fail("Arm effort feedback is unavailable during handle approach")
        if max(abs(value) for value in values) > 90.0:
            self.fail("Arm effort exceeds 90 percent; stopping cabinet manipulation")
        return values

    def _observe(self, step):
        self._effort()
        self._ensure_wrist_level()
        # Capture AFTER measured leveling/action completion; reject missing or
        # stalled feeds rather than showing a pre-motion view to the model.
        frames = {}
        for camera in ("head", "wrist"):
            previous = self.main_image if camera == "head" else self.wrist_image
            frames[camera] = self._next_image(camera, previous)
            if frames[camera] is None:
                self.fail(f"Missing {camera} camera frame")
            self._save_frame(frames[camera], camera, f"gpt_{step}")
        pose = self.manipulation.pose
        self._effort()
        observation = {
            "step": step,
            "wrist_xyz_m": list(pose.position),
            "wrist_rpy_rad": list(pose.rpy),
            "base_odom_xyt": list(self._odom_xyt()),
            "joint_names": list(self.joint_states.name),
            "joint_positions_rad": list(self.joint_states.position),
            "joint_efforts": list(self.joint_states.effort),
            "grip_commanded": self._grip_commanded,
        }
        self.debug_event("gpt_observation", **observation)
        return observation, frames

    def _act(self, action, values):
        self.check_cancelled()
        self._effort()
        if action == "move_wrist":
            current = self.manipulation.pose.position
            if math.dist(current, values) > 0.030001:
                raise ValueError("Wrist target is more than 3 cm from measured pose")
            if not (0.10 <= values[0] <= 0.40 and abs(values[1]) <= 0.20 and 0.12 <= values[2] <= 0.40):
                raise ValueError("Wrist target outside cabinet workspace")
            # Unreachable target is model feedback; a missed executed pose is
            # a hard failure. Do not conceal tracking failures as retryable IK.
            self._level_ik.solve(values, tuple(self.joint_states.position[:5]))
            self._move_wrist(values, 1.0)
        elif action == "base_step":
            if self._base_travel + abs(values[0]) > 1.0:
                raise ValueError("One-metre cumulative base travel budget exhausted")
            self._drive(values[0])
            self._base_travel += abs(values[0])
        elif action == "base_turn":
            if self._grip_commanded:
                raise ValueError("Release gripper before turning the base")
            if self._base_turn + abs(values[0]) > 1.2:
                raise ValueError("Cumulative base turn budget exhausted")
            self._rotate(values[0])
            self._base_turn += abs(values[0])
        elif action == "close_gripper":
            self.manipulation.gripper_close(0.60, duration=1.0)
            self._grip_commanded = True
        elif action == "open_gripper":
            self.manipulation.gripper_open(duration=1.0)
            self._grip_commanded = False
        elif action == "done" and self._grip_commanded:
            raise ValueError("Release and observe the door before reporting done")
        self.sleep(0.15)

    def execute(self, max_steps: int = 60) -> SkillReturn:
        """Run at most max_steps observations/decisions (1–100)."""
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 100:
            self.fail("max_steps must be an integer between 1 and 100")
        try:
            policy = self._make_policy()  # Fail for missing credentials BEFORE any motion.
        except ValueError as error:
            self.fail(str(error))
        self.debug_event("policy_configured", model=policy.model, backend=policy.backend)
        self._frame_index = 0
        self._grip_commanded = False
        self._base_travel = 0.0
        self._base_turn = 0.0
        rejected = 0
        try:
            self._effort()
            self._odom_xyt()
            if self.main_image is None or self.wrist_image is None:
                self.fail("Both cameras are required before staging")
            self.head.set_position(0)
            self.manipulation.torque_on()
            # Empty gripper and a clear arm path are documented preconditions.
            self.manipulation.gripper_open(duration=1.0)
            self._move_wrist((0.30, 0.0, 0.30), 3.0)
            for step in range(max_steps):
                observation, frames = self._observe(step)
                call_id, (action, values, note) = policy.decide(observation, frames, self.sleep)
                self.check_cancelled()
                self.feedback(f"{policy.model} cabinet {step + 1}: {action} — {note}")
                self.debug_event("gpt_action", model=policy.model, action=action, values=list(values), note=note)
                try:
                    self._act(action, values)
                except ValueError as error:
                    rejected += 1
                    policy.result(call_id, f"Rejected before motion: {error}")
                    if rejected >= 3:
                        self.fail("Three consecutive rejected actions; inspect the debug trace")
                    continue
                rejected = 0
                policy.result(call_id, "Action completed; use the next measured observation to assess its effect")
                if action == "give_up":
                    self.fail(f"{policy.model} stopped without verifying an open cabinet: {note}")
                if action == "done":
                    return f"{policy.model} visually reports the cabinet open after {policy.calls} decisions: {note}"
            self.fail(f"Decision budget exhausted ({max_steps}); cabinet opening not verified")
        except (ArmFailed, ArmUnhealthy, ValueError, RuntimeError, OSError) as error:
            self.fail(str(error))
        finally:
            # Preserve the gripper on cancellation/failure: it might hold the
            # handle. No automatic opening or folding into the door.
            self.mobility.stop()
            self.manipulation.stream_stop()

    def _odom_xyt(self):
        pose = self.mobility.odom_xyt(self.odom)
        if pose is None:
            self.fail("Odometry is required for handle acquisition")
        return pose

    def _save_frame(self, image, camera: str, label: str):
        frame_name = f"{self._frame_index:02d}_{camera}_{label}.jpg"
        self._frame_index += 1
        directory = self.debug_directory
        if directory is not None:
            try:
                (directory / frame_name).write_bytes(image.jpeg)
            except Exception as error:  # noqa: BLE001 - observability must not block safety
                self.logger.warning(f"[OpenCabinetWithGpt] could not save {frame_name}: {error}")
        return frame_name

    def _next_image(self, camera, previous, timeout=1.5):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            image = self.main_image if camera == "head" else self.wrist_image
            if image is not previous:
                return image
            self.sleep(0.04)
        self.fail(f"No fresh {camera} camera frames while measuring the handle")

    def _rotate(self, radians):
        before = self._odom_xyt()
        if not self.mobility.rotate_by(self._odom_xyt, radians, logger=self.logger):
            self.fail("Base rotation failed during handle acquisition")
        self.debug_event("base_rotation", requested_rad=radians, before=list(before), after=list(self._odom_xyt()))

    def _drive(self, metres):
        before = self._odom_xyt()
        if not self.mobility.drive(
            self._odom_xyt, metres, kp=2.0, v_min=0.01, v_max=0.06, tolerance=0.003, logger=self.logger
        ):
            self.fail("Base motion failed during handle acquisition")
        after = self._odom_xyt()
        forward = (after[0] - before[0]) * math.cos(before[2]) + (after[1] - before[1]) * math.sin(before[2])
        self.debug_event(
            "base_translation", requested_m=metres, measured_forward_m=forward, before=list(before), after=list(after)
        )
        self.logger.info(f"[handle base] requested={metres:+.3f}m measured forward={forward:+.3f}m")
        if abs(forward - metres) > 0.005:
            self.fail(f"Base missed requested travel: requested {metres:+.3f}m, measured {forward:+.3f}m")

    def _move_wrist(self, target, duration):
        """Reject missed poses instead of accumulating fictitious progress."""
        self._effort()  # Reject stale arm telemetry before planning.
        try:
            joints = self._level_ik.solve(target, tuple(self.joint_states.position[:5]))
        except ValueError as error:
            self.fail(str(error))
        self.debug_event("level_wrist_plan", target=list(target), joints=list(joints))
        self.check_cancelled()
        self.manipulation.move_joints(joints, duration=duration)
        self.sleep(0.15)  # Allow a fresh FK sample after joint motion completes.
        settled = self.manipulation.pose
        error = tuple(settled.position[i] - target[i] for i in range(3))
        roll, pitch, _yaw = settled.rpy
        self.debug_event(
            "wrist_tracking",
            target=list(target),
            measured=list(settled.position),
            error=list(error),
            measured_rpy=list(settled.rpy),
        )
        self.logger.info(
            f"[handle tracking] xyz error={tuple(round(v, 3) for v in error)}m "
            f"roll={math.degrees(roll):+.1f}deg pitch={math.degrees(pitch):+.1f}deg"
        )
        if (
            not all(math.isfinite(v) for v in (*error, roll, pitch))
            or max(abs(v) for v in error) > 0.010
            or max(abs(roll), abs(pitch)) > math.radians(5)
        ):
            self.manipulation.stream_stop()
            self.fail("Arm did not reach the level wrist target; stopping instead of accumulating missed motion")
        return settled

    def _ensure_wrist_level(self):
        """Verify measured orientation before interpreting the wrist image."""
        pose = self.manipulation.pose
        roll, pitch, _yaw = pose.rpy
        if not all(math.isfinite(value) for value in (roll, pitch)):
            self.fail("Cannot verify gripper is horizontal: invalid orientation feedback")
        corrected = max(abs(roll), abs(pitch)) > math.radians(5)
        if corrected:
            self.logger.info("[handle] leveling gripper before wrist vision")
            self._move_wrist(tuple(pose.position), 0.8)
            # Read actual feedback again; a requested level pose is not proof.
            pose = self.manipulation.pose
            roll, pitch, _yaw = pose.rpy
        if not all(math.isfinite(value) for value in (roll, pitch)) or max(abs(roll), abs(pitch)) > math.radians(5):
            self.fail("Gripper is not horizontal; wrist vision is paused")
        self.logger.info(
            f"[handle] horizontal verified: roll={math.degrees(roll):+.1f}deg pitch={math.degrees(pitch):+.1f}deg"
        )
        self.debug_event("wrist_horizontal_verified", roll=roll, pitch=pitch, corrected=corrected)
        return corrected
