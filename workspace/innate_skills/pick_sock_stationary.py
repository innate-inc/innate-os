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
            raise SkillFailed("Sock outside stationary arm reach; reposition the sock")
        if not self.manipulation.reachable(x, y, self._p["hover_z"], pitch=self._p["arm_pitch"]):
            raise SkillFailed("Sock hover unreachable without driving; reposition the sock")
        return PickAnyObject._grasp_at(self, prompt, xy)

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
