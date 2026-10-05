# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini sock detection and the fast arm grasp, with no base approach."""

import math

from innate_skills.pick_any_object import NAV_ARM, PickAnyObject
from innate_skills.pick_sock_fast import PickSockFast

from innate import Llm, SkillReturn
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W, pixel_to_floor


class PickSockStationary(PickSockFast):
    """Pick a described sock already within arm reach. Never move the wheels."""

    llm: Llm = Llm("google:gemini-3.6-flash", thinking="minimal")

    def _grasp_at(self, prompt, xy):
        # Do not use PickSockFast's +/-6 cm base correction, or silently clamp
        # a distant sock to a different grasp location.
        x, y = xy[0] - self._p["grasp_x_off"], xy[1]
        cx, cy = self.manipulation.clamp_reach(x, y)
        if not all(math.isfinite(v) for v in (x, y, cx, cy)) or math.hypot(cx - x, cy - y) > 0.002:
            raise SkillFailed(
                f"Sock outside stationary arm reach at x={x:.2f}m y={y:.2f}m; reposition or turn toward it"
            )
        self.logger.info(f"[StationarySock] floor_xy={xy!r} grasp_xy=({x:.3f},{y:.3f})")
        original = self._p
        # The 15 cm transit waypoint is optional. A nearby sock may have a
        # valid low approach even when that high pose has no IK solution.
        hover = next(
            (
                z
                for z in (original["hover_z"], 0.12, 0.10)
                if self.manipulation.reachable(x, y, z, pitch=original["arm_pitch"])
            ),
            None,
        )
        if hover is None or not self.manipulation.reachable(x, y, 0.08, pitch=original["arm_pitch"]):
            raise SkillFailed(
                f"No stationary arm approach at x={x:.2f}m y={y:.2f}m; reposition or turn toward the sock"
            )
        self.logger.info(f"[StationarySock] selected hover={hover:.2f}m; base remains stopped")
        self._p = {**original, "hover_z": hover}
        try:
            return PickAnyObject._grasp_at(self, prompt, xy)
        finally:
            self._p = original

    def execute(self, prompt: str) -> SkillReturn:
        """Pick the described floor sock, including its color, without base motion."""
        if not prompt.strip():
            raise SkillFailed("Describe the sock to pick, including its color")
        self._target_description = prompt.strip()
        self._grip_strength = None
        self._holding = self._carried = False
        self._last_seen = None
        self._coasts = 0
        self.mobility.stop()
        try:
            if not self.llm.available:
                raise SkillFailed("Gemini detection is unavailable")
            self.check_cancelled()
            self.head.set_position(int(round(self._p["tilt_deg"])))
            self.manipulation.move_joints(NAV_ARM, duration=self._p["nav_arm_s"])
            self.overlay.begin(prompt, stages=["search", "grasp", "verify"], frame=(IMG_W, IMG_H))
            px = self._detect_px(self._target_description)
            if px is None:
                raise SkillFailed("Sock not visible; turn in place or reposition the sock")
            xy = pixel_to_floor(*px, self._p["tilt_deg"])
            if xy is None or not all(math.isfinite(v) for v in xy):
                raise SkillFailed("Could not localize the floor sock")
            self.check_cancelled()
            self._grasp_at(self._target_description, xy)
            if not self._grasp_verified(self._target_description, None):
                self._holding = False
                raise SkillFailed("Empty sock grasp")
            return f"Picked up '{self._target_description}' without moving the base; gripper verified."
        finally:
            self.mobility.stop()
            self._rest_arm(keep_grip=self._holding)
            self.head.set_position(0)
