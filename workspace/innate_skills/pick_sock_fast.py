# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math

from innate_skills.approach import FloorApproach, ask_head, base_to_odom, odom_to_base
from innate_skills.drop_in_box_fast import DEMO_CARRY_JOINTS
from innate_skills.pick_any_object import PARAMS, PickAnyObject
from innate_skills.sock_grip import fresh_sock_held

from innate import SkillReturn, Waypoint, vision
from innate.exceptions import ArmFailed, ArmUnhealthy, SkillFailed


class PickSockFast(PickAnyObject):
    """Pick up a sock from the floor outside the box. Takes no arguments."""

    # Sweep through the 15 cm hover to 8 cm; retain the final 5 cm
    # floor descent at the established nominal 0.08 m/s.
    # Keep the established floor target and pre-close un-press for fabric.
    _p = {
        **PARAMS,
        "wrist_steps": 0.0,
        "descend_z1": 0.15,
        "descend_z2": 0.15,
        "descend_z3": 0.15,
        "descend_s": 1.5,
        "nav_arm_s": 0.8,
        "hover_s": 0.8,
        "lift_s": 0.8,
        "twist_s": 0.6,
        "fallback_lift_s": 1.0,
        "fold_s": 0.9,
        "bearing_go_deg": 12.0,
        "accept_tracked_arrival": True,
        "trust_sock_tracking": True,
        "local_visual_recovery": True,
        "stable_arrival_margin": True,
        "arrival_tracking_spread_m": 0.02,
        "verify_in_place": True,
        "skip_carry_repeat": True,
        "carry_joints": DEMO_CARRY_JOINTS,
        "close_lift_tolerance_m": 0.0005,
        "close_lift_s": 0.6,
        "close_s": 0.7,
        "rot_kp": 2.4,
        "rot_wz_max": 0.9,
        "rot_wz_min": 0.2,
        "ease_base_motion": True,
        "fast_far_approach": True,
        "metric_sock_approach": True,
        "reuse_enabled_torque": True,
    }

    @property
    def _soft_object(self):
        return True

    def execute(self) -> SkillReturn:
        """Pick the sock from the floor outside the box."""
        return super().execute("the sock on the floor outside the box")

    def _detection_question(self, selection):
        return (
            "Find a sock lying on the floor OUTSIDE any box, not held by the robot. "
            f"Return only a JSON list for {selection}: "
            '{"box_2d":[ymin,xmin,ymax,xmax],"grasp_point":[y,x]} in 0-1000 coordinates. '
            "Grasp point is the center of the visible sock. Empty list if absent."
        )

    def _parse_detections(self, text):
        return vision.parse_det_cands_boxed(text)

    def _detect_px(self, prompt):
        # Re-detections retain all candidates so the existing identity gate can
        # keep the chosen sock rather than silently switching to a nearby one.
        selection = "one best match" if self._last_seen is None else "all matching socks"
        text, img = ask_head(
            self,
            self._detection_question(selection),
            self._p["settle_s"],
        )
        self._local_detection_box = None
        self._local_detection_image = img
        cands = self._parse_detections(text)
        cand = self._choose_cand(cands) if cands else None
        if cand is None:
            self.overlay.clear("target")
            return None
        u, v, _grip, box = cand
        self._grip_strength = self._p["close_strength"]
        self._local_detection_box = box
        seen = self._sighting(cand)
        if seen is not None:
            self._last_seen = seen
        self._draw_sighting((u, v), box, seen[2] if seen else None, len(cands))
        return u, v

    def _approach_grasp(self, x, y, z):
        # Verify the claw before the sweep so it does not pause at hover.
        self._claw_open()
        self.check_cancelled()
        end_z = 0.08
        self.manipulation.follow(
            [
                Waypoint(x, y, z, pitch=self._p["arm_pitch"], duration=self._p["hover_s"]),
                Waypoint(x, y, end_z, pitch=self._p["arm_pitch"], duration=0.875),
            ],
            grip=self.manipulation.GRIPPER_OPEN,
        )
        # follow() returns an unverified pose. Do not treat a failed free-space
        # approach as expected floor contact or continue closing in mid-air.
        pose = self.manipulation.pose
        if not all(math.isfinite(v) for v in (pose.x, pose.y, pose.z)) or (
            math.hypot(pose.x - x, pose.y - y) > 0.03 or abs(pose.z - end_z) > 0.02
        ):
            raise ArmUnhealthy("Sock free-space approach did not reach the descent pose")
        return end_z

    def _push_to_floor(self, x, y, z_from, roll, pitch, yaw):
        original = self._p
        # Only shorten the remaining 5 cm after the successful free-space
        # sweep. Retries start higher and keep their original descent timing.
        if abs(z_from - 0.08) < 1e-6:
            self._p = {**original, "descend_s": 0.625}
        try:
            return super()._push_to_floor(x, y, z_from, roll, pitch, yaw)
        finally:
            self._p = original

    def _close_grip(self):
        joints = self._arm_joints()
        if not all(math.isfinite(j) for j in joints):
            raise ArmFailed("Invalid joint feedback before close-and-twist")
        grip = -self._p["close_strength"]
        # Close for 0.4 s before twisting. The remaining closure overlaps the
        # 0.6 s twist. Keep the other arm joints fixed until the separate lift.
        lead = list(joints)
        lead[5] = joints[5] + (grip - joints[5]) * (0.4 / 0.7)
        end = list(joints)
        twist = self._p["twist_rad"] if joints[4] + self._p["twist_rad"] <= 1.4 else -self._p["twist_rad"]
        end[4] = max(-1.4, min(1.4, joints[4] + twist))
        end[5] = grip
        # Existing SDK trajectory transport supports all six joints; follow()
        # only supports a constant grip. Latch preload BEFORE dispatch so a
        # partially executed failure cannot reopen the claw during teardown.
        self.check_cancelled()
        self._holding = True
        self.manipulation._grip_target = grip
        if not self.manipulation._send_trajectory([lead, end], [0.4, self._p["twist_s"]]):
            raise ArmFailed("Sock close-and-twist trajectory failed")
        return True

    def _close_twist_lift(self, x, y, roll, pitch, yaw):
        for attempt in range(3):
            self.check_cancelled()
            super()._close_twist_lift(x, y, roll, pitch, yaw)
            if fresh_sock_held(self):
                return
            self._holding = False
            if attempt == 2:
                raise SkillFailed("Empty sock grasp after three attempts")
            self.overlay.readout("empty grasp; retrying")
            self._claw_open()
            self.check_cancelled()
            self._push_to_floor(x, y, self.manipulation.pose.z, roll, pitch, yaw)

    def _grasp_verified(self, prompt, approach):
        self.mobility.stop()
        self.overlay.stage("verify")
        # Checked immediately after lifting above; check again after folding
        # to catch a sock that slips during the carry motion, with no model.
        self._fold_to_carry()
        self._join_fold()
        return fresh_sock_held(self)

    def _hover_reachable(self, xy):
        p = self._p
        x, y = self.manipulation.clamp_reach(xy[0] - p["grasp_x_off"], xy[1])
        return self.manipulation.reachable(x, y, p["hover_z"], pitch=p["arm_pitch"])

    def _grasp_at(self, prompt, xy):
        # Correct the base before lowering the arm, without another agent turn.
        if not self._hover_reachable(xy):
            approach = FloorApproach(self, {**self._p, "drive_tol_m": 0.003}, self._detect_px)
            target = base_to_odom(approach.odom_xyt(), xy)
            if target is None:
                raise SkillFailed("Pickup pose unreachable and no odometry for a local correction")
            correction = next(
                (d for d in (0.02, 0.04, 0.06, -0.02, -0.04, -0.06) if self._hover_reachable((xy[0] - d, xy[1]))), None
            )
            if correction is None:
                raise SkillFailed("No reachable pickup hover within a 6 cm base correction")
            self.check_cancelled()
            if not approach.drive(correction):
                raise SkillFailed("Pickup position correction did not complete")
            xy = odom_to_base(approach.odom_xyt(), target)
            if xy is None or not self._hover_reachable(xy):
                raise SkillFailed("Pickup hover remains unreachable after local correction")
        return super()._grasp_at(prompt, xy)
