# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.approach import FloorApproach
from innate_skills.pick_sock_yoloe import PickSockYoloe
from innate_skills.sock_box_exclusion import SockBoxExclusion

import json
from innate import Llm, SkillReturn
from innate_skills.sock_contact import ContactSelection, error_details
from innate.geometry import floor_to_pixel, pixel_to_floor


class PickSockPileYoloe(PickSockYoloe):
    """Find a floor pile and refine a fabric contact point during the approach."""

    contact_llm = Llm("openai-chat:qwen3.8-flash-next", thinking="minimal", extra_body=json.dumps({
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": "contact", "strict": True,
            "schema": {"type": "object", "properties": {"id": {"type": "integer"}},
                       "required": ["id"], "additionalProperties": False}}}}))

    detection_prompt = "a pile of socks"
    detection_prompts = ("a pile of socks", "a pile of clothes")
    # The rehearsed pile is left of the box; positive yaw searches left.
    _p = {**PickSockYoloe._p, "search_turns_deg": (0, 90, 90, 90)}

    def execute(self) -> SkillReturn:
        """Try to grasp several socks together from a floor pile; count is not verified."""
        self._box_exclusion = SockBoxExclusion(self)
        self._contact = None
        try:
            return super().execute()
        finally:
            if self._contact is not None:
                self._contact.close()
                self._contact = None

    def _detect_px(self, prompt):
        if self._contact is not None:
            self._contact.close()
            self._contact = None
        point = super()._detect_px(prompt)
        if point is None:
            return None
        try:
            payload = self._yolo_payload
            # Match the chosen target, not a higher-confidence rejected box.
            size = payload["image"]
            if (size["width"], size["height"]) != (640, 480):
                self.logger.info('[SockContact] fallback reason=unsupported_image_size')
                return point
            box = self._local_detection_box
            item = next(d for d in payload["detections"]
                        if all(abs(a-b) < .01 for a,b in zip(d["bbox_xyxy"], box)))
            provider = self.contact_llm._provider()
            self._contact = ContactSelection(self._local_detection_image, item["mask_png_base64"], box,
                                             provider, lambda: self.main_image, self.logger)
            self.logger.info('[SockContact] started Qwen and visual tracking during approach')
        except Exception as exc:
            self.logger.info('[SockContact] setup_failed ' + json.dumps(error_details(exc)))
        return point

    def _refined_approach_pixel(self, raw, xy, arrived):
        if self._contact is None or xy is None or xy[0] > .50:
            return None
        result = self._contact.take(raw, arrived)
        if result is not None and result[0] == 'point':
            pixel, _ = result[1]
            floor = pixel_to_floor(*pixel, self._p['tilt_deg'])
            hull = self._box_exclusion.hull(raw)
            if floor is None or self._box_exclusion.contains(hull, pixel):
                self._contact.log('fallback', reason='no_floor_projection' if floor is None else 'inside_box')
                return None
            self._contact.log('adopted', current_pixel=list(pixel), floor_xy=list(floor))
        return result

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
