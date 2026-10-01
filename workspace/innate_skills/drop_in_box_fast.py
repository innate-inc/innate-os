# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Drop from above into the demo's known 10–12 cm open box."""

import math

from innate_skills.approach import ask_head
from innate_skills.drop_in_box import PARAMS, DropInBox
from innate_skills.sock_grip import closing_command, fresh_sock_held

from innate import SkillReturn, vision
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W

# Match the reachable servo-limited pose, not the old out-of-range request.
DEMO_CARRY_JOINTS = [-1.5708, 0.31, -1.5708, 1.70, -0.26]


class DropInBoxFast(DropInBox):
    """Put the held sock into the open plastic box. Takes no arguments."""

    _p = {
        **PARAMS,
        "carry_s": 0.8,
        "hover_s": 0.8,
        "release_settle_s": 0.4,
        "accept_tracked_arrival": True,
        "retreat_speed_scale": 3.0,
        "local_visual_recovery": True,
        "recovery_box_edges": True,
        "robust_floor_tracking": True,
        "travel_joints": DEMO_CARRY_JOINTS,
        "rot_kp": 2.4,
        "rot_wz_max": 0.9,
        "rot_wz_min": 0.2,
        "ease_base_motion": True,
        "fast_far_approach": True,
        "clearance_s": 0.6,
        "rest_fold_s": 0.9,
    }
    RELEASE_Z = 0.24  # wrist/ee height; fingertips extend below this
    CLEARANCE_Z = 0.26

    def execute(self) -> SkillReturn:
        """Drop the held sock into the visible low open demo box."""
        return super().execute("the open plastic box on the floor")

    def _detect_px(self, prompt):
        self.overlay.readout("looking for the box", busy=True)
        text, img = ask_head(
            self,
            "Find the open plastic box standing on the floor. Ignore the robot arm. "
            'Return only a JSON list with {"box_2d":[ymin,xmin,ymax,xmax]} normalized '
            "0–1000, tight around the whole box including its bottom. Empty list if absent.",
            self._p["settle_s"],
        )
        self._local_detection_image = img
        self._local_detection_box = None
        box = vision.parse_det_box(text)
        if box is None:
            self._box_u = self._box_top_v = None
            self.overlay.clear("target", "rim")
            return None
        x, y, w, h = box
        self._local_detection_box = (x, y, x + w, y + h)
        self._near_rim_v = None
        self._rim_z = 0.12  # supplied demo assumption, not a measurement
        self._box_u = min(float(IMG_W - 1), x + w / 2)
        self._box_top_v = y
        self._draw_container((x, y, x + w, y + h))
        return self._park_if_clipped((self._box_u, min(float(IMG_H - 1), y + h)), y + h)

    def _release_xy(self, near_x, near_y):
        return self.manipulation.clamp_reach(near_x + self._p["drop_inset"], near_y)

    def _release_at(self, near_x, near_y):
        p = self._p
        x, y = self._release_xy(near_x, near_y)
        if x < near_x + p["drop_inset_min"]:
            raise SkillFailed("Cannot reach inside the box from here")
        self.overlay.stage("release")
        if self.manipulation.torque_enabled is not True:
            self.manipulation.torque_on()
        # First raise, then reach across from above. Do not continue from a
        # refused or low approach pose as the generic best-effort helper does.
        raised = self.manipulation.move_to(
            p["carry_x"],
            y,
            0.28,
            pitch=p["arm_pitch"],
            duration=p["carry_s"],
            tolerance_xy=None,
            tolerance_z=None,
        )
        self._require_pose(raised, p["carry_x"], y, 0.28)
        self.check_cancelled()
        # Latch before entering the footprint, including a failed/partial move.
        self._over_rim = True
        self._clearance_z = self.CLEARANCE_Z
        posed = self.manipulation.move_to(
            x,
            y,
            self.RELEASE_Z,
            pitch=p["arm_pitch"],
            duration=p["hover_s"],
            tolerance_xy=None,
            tolerance_z=None,
        )
        self._require_pose(posed, x, y, self.RELEASE_Z)
        self.check_cancelled()
        self.manipulation.gripper_open(duration=0.6)
        self._released = True
        self.sleep(p["release_settle_s"])
        self._lift_out()
        return x, y, self.RELEASE_Z

    @staticmethod
    def _require_pose(pose, x, y, z):
        if (
            pose is None
            or not all(math.isfinite(v) for v in (pose.x, pose.y, pose.z))
            or math.hypot(pose.x - x, pose.y - y) > 0.03
            or abs(pose.z - z) > 0.01
        ):
            raise SkillFailed("Arm did not reach the overhead drop pose; refusing to release")

    def _secure_grip(self):
        if closing_command(self.manipulation):
            if self.manipulation.torque_enabled is not True:
                self.manipulation.torque_on()
            return True
        return super()._secure_grip()

    def _holding(self, closed):
        return closed and fresh_sock_held(self)

    def _carry_pose(self, joints):
        js = self.joint_states
        if (
            js is not None
            and len(js.position) >= 5
            and all(math.isfinite(a) and abs(a - b) <= 0.05 for a, b in zip(js.position[:5], joints, strict=False))
        ):
            self.logger.info("[DropInBoxFast] carry shortcut accepted: already at travel pose")
            return
        measured = list(js.position[:5]) if js is not None else None
        self.logger.info(f"[DropInBoxFast] carry shortcut fallback: measured={measured}, target={joints}")
        return super()._carry_pose(joints)
