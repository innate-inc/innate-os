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
        return PickAnyObject.execute(self, "the sock on the floor")

    def _detection_question(self, selection):
        return (
            "Find the sock on the floor. Return " + selection + ". "
            'Output ONLY a JSON list of {"box_2d":[ymin,xmin,ymax,xmax]}. '
            "Coordinates are integers from 0 to 1000: vertical first, horizontal second. "
            "Use tight bounding boxes. If none are visible, output []."
        )

    def _parse_detections(self, text):
        # Qwen can swap the axes of a separate grasp_point. The rehearsed sock
        # target is its center, so derive that from a validated box instead.
        boxes = []
        for det in vision.parse_dets(text):
            box = det.get("box_2d") if isinstance(det, dict) else None
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                continue
            if not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1000
                for v in box
            ):
                continue
            if box[0] >= box[2] or box[1] >= box[3]:
                continue
            boxes.append({"box_2d": box})
        return vision.parse_det_cands_boxed(json.dumps(boxes))
