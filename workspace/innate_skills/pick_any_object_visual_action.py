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
        self._visual_abort = None
        try:
            return super().execute(prompt=prompt, controller="astra")
        finally:
            # Bounded diagnostic snapshot is written only after control/cleanup,
            # so storage I/O cannot age an observation before an arm command.
            if self._visual_abort is not None:
                try:
                    self.storage["last_visual_abort"] = self._visual_abort
                except Exception:
                    self.logger.warning("Unable to save visual abort diagnostics")

    def _observe_pickup(self, prompt, image, view):
        self._pickup_policy.max_calls = MAX_ASTRA_CALLS
        return super()._observe_pickup(prompt, image, view)

    def _fresh_wrist_frame(self, timeout=1.0):
        previous = self.wrist_image
        deadline = time.monotonic() + timeout
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

    @staticmethod
    def _view_change(frame, current, pose, after):
        if not frame.capture_is_current() or current.capture_generation != frame.capture_generation:
            return "camera generation changed"
        if not all(math.isfinite(v) for v in (*after.position, *after.rpy)):
            return "nonfinite arm pose"
        if math.dist(pose.position, after.position) > 0.002:
            return "arm position changed"
        if max(abs(a - b) for a, b in zip(pose.rpy, after.rpy, strict=True)) > 0.01:
            return "arm orientation changed"
        if not unchanged(frame, current):
            return "wrist image changed"
        return None

    def _remember_visual_abort(self, reason, frame, current, pose, after):
        def snapshot(image, arm):
            return {
                "jpeg_b64": str(image) if image is not None else None,
                "camera": {
                    name: getattr(image, name, None)
                    for name in ("capture_ns", "received_ros_ns", "received_monotonic", "capture_generation")
                },
                "position": list(arm.position) if arm is not None else None,
                "rpy": list(arm.rpy) if arm is not None else None,
            }

        self._visual_abort = {
            "reason": reason,
            "wall_ns": time.time_ns(),
            "checked_monotonic": time.monotonic(),
            "original_pose_sampled_monotonic": getattr(self, "_decision_pose_monotonic", None),
            "current_pose_sampled_monotonic": getattr(self, "_current_pose_monotonic", None),
            "original": snapshot(frame, pose),
            "current": snapshot(current, after),
        }

    def _settled_wrist_observation(self):
        # Observe the existing hardware settling interval instead of sleeping
        # blindly. Compare every new sample to one anchor, so slow cumulative
        # drift cannot pass merely because adjacent frames look similar.
        window = self._p["wrist_settle_s"]
        if not math.isfinite(window) or window <= 0:
            raise SkillFailed("Invalid wrist settling interval")
        deadline = time.monotonic() + 4 * window
        self._decision_pose_monotonic = None
        anchor = arm_anchor = None
        frame = pose = None
        last_capture_ns = None
        while time.monotonic() < deadline:
            try:
                frame = self._fresh_wrist_frame(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
            except SkillFailed:
                self._remember_visual_abort("no fresh settling frame", anchor, self.wrist_image, arm_anchor, None)
                raise
            pose = self.manipulation.pose  # sampled after receiving this capture
            sampled = time.monotonic()
            self._current_pose_monotonic = sampled
            if not all(math.isfinite(v) for v in (*pose.position, *pose.rpy)):
                self._remember_visual_abort("nonfinite settling pose", anchor, frame, arm_anchor, pose)
                raise SkillFailed("Nonfinite settling pose")
            reason = self._view_change(anchor, frame, arm_anchor, pose) if anchor is not None else None
            if reason == "camera generation changed":
                self._remember_visual_abort(reason, anchor, frame, arm_anchor, pose)
                raise SkillFailed(reason)
            gap = last_capture_ns is not None and (frame.capture_ns - last_capture_ns) * 1e-9 > 0.5
            last_capture_ns = frame.capture_ns
            if anchor is None or reason or gap:
                # A gap beyond the existing freshness horizon is not observed
                # stationarity: establish a new window from this fresh capture.
                anchor, arm_anchor = frame, pose
                self._decision_pose_monotonic = sampled
            elif (frame.capture_ns - anchor.capture_ns) * 1e-9 >= window:
                self._decision_pose_monotonic = sampled
                self.check_cancelled()
                return frame, pose
        self._remember_visual_abort("wrist did not settle", anchor, frame, arm_anchor, pose)
        raise SkillFailed("Wrist did not settle within the observation budget")

    def _stable_after_decision(self, frame, pose):
        try:
            current = self._fresh_wrist_frame()
        except SkillFailed:
            self._remember_visual_abort("no fresh validation frame", frame, self.wrist_image, pose, None)
            raise
        after = self.manipulation.pose
        self._current_pose_monotonic = time.monotonic()
        reason = self._view_change(frame, current, pose, after)
        if reason:
            self._remember_visual_abort(reason, frame, current, pose, after)
            raise SkillFailed("Visual decision rejected: " + reason)
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
            frame, pose = self._settled_wrist_observation()
            if not inside_envelope(pose.position, pose.rpy, minimum_z=self._p["floor_z"] - POSITION_TOLERANCE_M):
                raise SkillFailed("Arm pose outside visual action envelope")
            joints = self._arm_joints()
            if not math.isfinite(joints[5]) or joints[5] < self.manipulation.GRIPPER_OPEN - 0.05:
                raise SkillFailed("Visual action requires a verified open claw")
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
                        "floor_target_rpy_rad": [0.0, self._p["arm_pitch"], 0.0],
                        "floor_drop_m": round(max(0.0, pose.position[2] - self._p["floor_z"]), 3),
                        "moves_remaining": MAX_MOVES - moves,
                        "wrist_decisions_remaining": MAX_WRIST_DECISIONS - decision,
                    },
                    timeout=30,
                )["detections"]
                if len(result) != 1 or result[0]["action"] == "abort":
                    after = self.manipulation.pose
                    self._current_pose_monotonic = time.monotonic()
                    self._remember_visual_abort(
                        "model uncertain", frame, getattr(self, "wrist_image", None), pose, after
                    )
                    self._visual_abort["model_result"] = result
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
