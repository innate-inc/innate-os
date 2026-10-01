# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import math

from innate_skills.pick_any_object import PickAnyObject
from innate_skills.pick_sock_fast import PickSockFast

from innate import Llm, SkillReturn, vision


class PickSockQwen(PickSockFast):
    """Pick a floor sock with Qwen detection and the rehearsed fast motion."""

    llm: Llm = Llm(
        "openai-chat:qwen3.8-flash-next-iq4-xs-mtp3",
        extra_body='{"chat_template_kwargs":{"enable_thinking":false}}',
        thinking="minimal",
    )

    def execute(self) -> SkillReturn:
        """Pick the sock on the floor."""
        self._detection_number = 0
        return PickAnyObject.execute(self, "the sock on the floor")

    def _detection_question(self, selection):
        return (
            "Find the sock on the floor. Return " + selection + ". "
            'Output ONLY a JSON list of {"box_2d":[ymin,xmin,ymax,xmax]}. '
            "Coordinates are integers from 0 to 1000: vertical first, horizontal second. "
            "Use tight bounding boxes. If none are visible, output []."
        )

    def _detect_px(self, prompt):
        self._detection_number = getattr(self, "_detection_number", 0) + 1
        self.logger.info(
            f"[QwenDetection #{self._detection_number}] start: target={prompt!r}, previous_sighting={self._last_seen!r}"
        )
        pixel = super()._detect_px(prompt)
        self.logger.info(f"[QwenDetection #{self._detection_number}] selected_pixel={pixel!r}")
        return pixel

    def _parse_detections(self, text):
        # Qwen can swap the axes of a separate grasp_point. The rehearsed sock
        # target is its center, so derive that from a validated box instead.
        label = f"[QwenDetection #{getattr(self, '_detection_number', 0)}]"
        self.logger.info(f"{label} raw_reply={text!r}")
        boxes = []
        detections = vision.parse_dets(text)
        if not detections:
            self.logger.info(f"{label} no detection objects parsed; see raw_reply")
        for det in detections:
            box = det.get("box_2d") if isinstance(det, dict) else None
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                self.logger.info(f"{label} rejected box: expected four coordinates, got {box!r}")
                continue
            if not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1000
                for v in box
            ):
                self.logger.info(f"{label} rejected box: nonnumeric, nonfinite, or outside 0–1000: {box!r}")
                continue
            if box[0] >= box[2] or box[1] >= box[3]:
                self.logger.info(f"{label} rejected box: reversed or empty bounds: {box!r}")
                continue
            boxes.append({"box_2d": box})
        candidates = vision.parse_det_cands_boxed(json.dumps(boxes))
        self.logger.info(f"{label} parsed_candidates={candidates!r}")
        return candidates
