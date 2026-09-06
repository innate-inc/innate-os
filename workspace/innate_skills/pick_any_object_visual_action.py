# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Opt-in direct visual actions. No transported image anchor or depth targeting."""

import math
import time

from innate_skills.pick_any_object import PickAnyObject
from innate_skills.pickup_visual_action import (
    POSITION_TOLERANCE_M,
    at_floor_grasp,
    fresh,
    inside_envelope,
    trajectory,
    unchanged,
)

from innate import SkillReturn
from innate.exceptions import SkillFailed

# Astra-only budget: ordinary inherited held-object verification can use Gemini.
# Experiments must separately cap ALL provider requests, including verifier retries.
# One head decision and up to four wrist decisions; the head search may consume
# more than one, in which case the same total cap reduces the wrist budget.
MAX_ASTRA_CALLS = 5
MAX_WRIST_DECISIONS = 4
MAX_MOVES = 2


class PickAnyObjectVisualAction(PickAnyObject):
    """Experimental compact rigid pickup with bounded fresh-view arm decisions."""

    def execute(self, prompt: str = "the red cube") -> SkillReturn:
        return super().execute(prompt=prompt, controller="astra")

    def _observe_pickup(self, prompt, image, view):
        self._pickup_policy.max_calls = MAX_ASTRA_CALLS
        return super()._observe_pickup(prompt, image, view)

    def _fresh_stationary_frame(self):
        previous = self.wrist_image
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            self.sleep(0.04)
            current = self.wrist_image
            if (
                current is not None
                and fresh(current, time.monotonic())
                and (previous is None or getattr(current, "capture_ns", 0) > (getattr(previous, "capture_ns", 0) or 0))
            ):
                return current
        raise SkillFailed("No fresh captured wrist frame")

    def _stable_after_decision(self, frame, pose):
        current = self._fresh_stationary_frame()
        after = self.manipulation.pose
        if (
            not frame.capture_is_current()
            or current.capture_generation != frame.capture_generation
            or not all(math.isfinite(v) for v in (*after.position, *after.rpy))
            or math.dist(pose.position, after.position) > 0.002
            or max(abs(a - b) for a, b in zip(pose.rpy, after.rpy, strict=True)) > 0.01
            or not unchanged(frame, current)
        ):
            raise SkillFailed("Scene or arm changed during visual decision")
        self.check_cancelled()
        return current

    def _grasp_at(self, prompt, xy):
        self.check_cancelled()
        if self._grip_strength != 0.35 or self._search_clearance not in {"flat", "low"}:
            raise SkillFailed("Visual action prototype requires a compact low rigid target")
        self._metric_open_pregrasp = True
        self.manipulation.torque_on()
        self._prepare_wrist_search(math.atan2(xy[1], xy[0]))
        moves = 0
        self._pickup_policy.max_calls = MAX_ASTRA_CALLS
        for decision in range(MAX_WRIST_DECISIONS):
            pose = self.manipulation.pose
            if not inside_envelope(pose.position, pose.rpy, minimum_z=self._p["floor_z"] - POSITION_TOLERANCE_M):
                raise SkillFailed("Arm pose outside visual action envelope")
            joints = self._arm_joints()
            if not math.isfinite(joints[5]) or joints[5] < self.manipulation.GRIPPER_OPEN - 0.05:
                raise SkillFailed("Visual action requires a verified open claw")
            frame = self._fresh_stationary_frame()
            try:
                result = self._pickup_policy.locate(
                    prompt,
                    frame,
                    self.sleep,
                    self.check_cancelled,
                    view="wrist_action",
                    reference=self._head_reference,
                    state={
                        "position_xyz_m": [round(v, 3) for v in pose.position],
                        "rpy_rad": [round(v, 3) for v in pose.rpy],
                        "claw_angle_rad": round(joints[5], 3),
                        "floor_grasp_ready": at_floor_grasp(pose.position, pose.rpy, self._p),
                        "floor_drop_m": round(max(0.0, pose.position[2] - self._p["floor_z"]), 3),
                        "moves_remaining": MAX_MOVES - moves,
                        "wrist_decisions_remaining": MAX_WRIST_DECISIONS - decision,
                    },
                    timeout=30,
                )["detections"]
                if len(result) != 1 or result[0]["action"] == "abort":
                    raise SkillFailed("Visual action is uncertain; preserving open posture")
                action = result[0]
                if action["action"] in {"floor", "shift"}:
                    if moves >= MAX_MOVES:
                        raise SkillFailed("Visual movement budget exhausted")
                    path = trajectory(pose.position, pose.rpy, action, self._p)
                    for p in path:
                        self.check_cancelled()
                        if not self.manipulation.reachable(*p[:3], roll=p[3], pitch=p[4], yaw=p[5]):
                            raise SkillFailed("Visual movement is unreachable")
                    # IK is a blocking service; observations may age while it runs.
                    self._stable_after_decision(frame, pose)
                    for p in path:
                        self.check_cancelled()
                        self.manipulation.move_to(
                            *p[:3],
                            roll=p[3],
                            pitch=p[4],
                            yaw=p[5],
                            duration=0.5,
                            grip=self.manipulation.GRIPPER_OPEN,
                            tolerance_xy=0.008,
                            tolerance_z=0.008,
                        )
                        self.sleep(0)
                    moves += 1
                    continue
                self._stable_after_decision(frame, pose)
                # A close decision follows no further alignment/floor movement.
                # Once committed, existing close/lift is deliberately non-cancellable.
                j6 = self._arm_joints()[5]
                if not math.isfinite(j6) or j6 < self.manipulation.GRIPPER_OPEN - 0.05:
                    raise SkillFailed("Claw changed before visual close")
                final_pose = self.manipulation.pose
                if not at_floor_grasp(pose.position, pose.rpy, self._p) or not at_floor_grasp(
                    final_pose.position, final_pose.rpy, self._p
                ):
                    raise SkillFailed("Visual close requires the existing floor-grasp posture")
                self.check_cancelled()
                original = self._p
                self._p = {**original, "grasp_retries": 0.0}
                try:
                    self._metric_open_pregrasp = False
                    self._close_twist_lift(prompt, *final_pose.position[:2], *final_pose.rpy)
                finally:
                    self._p = original
                return
            except ValueError as error:
                raise SkillFailed(str(error)) from None
        raise SkillFailed("Visual decision budget exhausted without a verified close")
