# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.approach import FloorApproach
from innate_skills.pick_sock_yoloe import PickSockYoloe
from innate_skills.sock_box_exclusion import SockBoxExclusion

from innate import SkillReturn
from innate.geometry import floor_to_pixel, pixel_to_floor


class PickSockPileYoloe(PickSockYoloe):
    """Track and grasp one selected YOLO mask point on a floor pile."""


    detection_prompt = "a pile of socks"
    detection_prompts = ("a pile of socks", "a pile of clothes")
    # The rehearsed pile is left of the box; positive yaw searches left.
    _p = {**PickSockYoloe._p, "search_turns_deg": (0, 90, 90, 90)}

    def execute(self) -> SkillReturn:
        """Track and grasp the same YOLO mask point on a floor pile."""
        self._box_exclusion = SockBoxExclusion(self)
        if type(self).__name__ == "PickSockPileYoloe":
            self.logger.info("[SockTarget] YOLO mask point for tracking and grasp; Qwen refinement disabled")
        return super().execute()



    def _detect_candidates(self, prompt):
        candidates, image = super()._detect_candidates(prompt)
        filtered = self._box_exclusion.filter(candidates, image)
        self.logger.info(f"[YOLOE-selection] raw={len(candidates)} after_box_exclusion={len(filtered)}")
        return filtered, image

    def _grasp_at(self, prompt, xy):
        # Tracking can drift after initial selection. Re-enter the existing
        # floor search instead of grasping a tracked point inside the box.
        while True:
            self.check_cancelled()
            pixel = floor_to_pixel(*xy, self._p["tilt_deg"])
            hull = self._box_exclusion.hull(self.main_image)
            if pixel is None or not self._box_exclusion.contains(hull, pixel):
                return super()._grasp_at(prompt, xy)
            self.mobility.stop()
            self.logger.info("[SockBox] tracked grasp entered box; resuming floor search")
            approach = FloorApproach(self, self._p, self._detect_px)
            xy = approach.position_above(prompt, approach.search(prompt))

    def _choose_cand(self, candidates):
        # detection_candidates sorts descending by confidence; box exclusion
        # preserves that order. Select the first floor-projectable candidate.
        self._coasts = 0
        projected = [(c, pixel_to_floor(c[0], c[1], self._p["tilt_deg"])) for c in candidates]
        selected = next((c for c, floor in projected if floor is not None), None)
        for rank, (candidate, floor) in enumerate(projected, 1):
            u, v, _, box = candidate
            reason = "no_floor_projection" if floor is None else "selected" if candidate is selected else "lower_confidence"
            self.logger.info(
                f"[YOLOE-selection] confidence_rank={rank} selected={candidate is selected} "
                f"reason={reason} pixel=({u:.1f},{v:.1f}) box={box} floor={floor}"
            )
        return selected
