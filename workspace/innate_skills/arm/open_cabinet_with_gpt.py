# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Experimental camera/telemetry tool agent for the house cabinet fixture."""

import math

from innate import SkillReturn
from innate.exceptions import ArmFailed, ArmUnhealthy

from .cabinet_agent_policy import CabinetPolicy
from .open_door_with_vision import OpenDoorWithVision


class OpenCabinetWithGpt(OpenDoorWithVision):
    """Open the lower kitchen cabinet with GPT-6 Astra and camera feedback.

    Start facing the cabinet within about 60 cm, arm clear of obstacles and
    gripper empty. Experimental: developed for the house simulator fixture.
    Requires OPENAI_API_KEY on the skills server. Uses head/wrist images and
    measured poses, bounded level-arm/base actions, and visual verification.
    No scene-open command or privileged cabinet pose is exposed to the model.
    """

    def _effort(self):
        values = super()._effort()
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
            policy = CabinetPolicy()  # Fail for missing key BEFORE any motion.
        except ValueError as error:
            self.fail(str(error))
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
            self._move_wrist((0.24, 0.0, 0.25), 3.0)
            for step in range(max_steps):
                observation, frames = self._observe(step)
                call_id, (action, values, note) = policy.decide(observation, frames, self.sleep)
                self.check_cancelled()
                self.feedback(f"GPT cabinet {step + 1}: {action} — {note}")
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
                    self.fail(f"GPT stopped without verifying an open cabinet: {note}")
                if action == "done":
                    return f"GPT visually reports the cabinet open after {policy.calls} decisions: {note}"
            self.fail(f"Decision budget exhausted ({max_steps}); cabinet opening not verified")
        except (ArmFailed, ArmUnhealthy, ValueError, RuntimeError, OSError) as error:
            self.fail(str(error))
        finally:
            # Preserve the gripper on cancellation/failure: it might hold the
            # handle. No automatic opening or folding into the door.
            self.mobility.stop()
            self.manipulation.stream_stop()
