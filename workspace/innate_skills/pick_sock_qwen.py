# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import math
import re

from innate_skills.pick_sock_fast import PickSockFast

from innate import Llm, SkillReturn, vision


class PickSockQwen(PickSockFast):
    """Pick a floor sock with Qwen detection and the rehearsed fast motion."""

    llm: Llm = Llm(
        "openai-chat:qwen3.8-flash-next-iq4-xs-mtp3",
        base_url="http://100.107.224.18:8081/v1",
        extra_body=json.dumps(
            {
                "chat_template_kwargs": {"enable_thinking": False},
                "max_tokens": 384,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "sock_detections",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "detections": {
                                    "type": "array",
                                    "maxItems": 4,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            name: {"type": "integer", "minimum": 0, "maximum": 1000}
                                            for name in ("x_min", "y_min", "x_max", "y_max")
                                        },
                                        "required": ["x_min", "y_min", "x_max", "y_max"],
                                        "additionalProperties": False,
                                    },
                                }
                            },
                            "required": ["detections"],
                            "additionalProperties": False,
                        },
                    },
                },
            }
        ),
        thinking="minimal",
    )

    def execute(self, prompt: str) -> SkillReturn:
        """Pick the described floor sock. Include its color, e.g. 'the blue sock'."""
        self._detection_number = 0
        return super().execute(prompt)

    def _detection_question(self, selection):
        return (
            f"Find {self._target_description!r} on the floor. Match its color and description. Return "
            + selection
            + ". "
            'Return {"detections":[{"x_min":...,"y_min":...,"x_max":...,"y_max":...}]}. '
            "Coordinates are integers from 0 to 1000. x is horizontal from the left; "
            "y is vertical from the top. Use tight bounding boxes. "
            'If none are visible, return {"detections":[]}.'
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
        try:
            # Accept fenced JSON from compatible servers, but never guess an
            # ambiguous array's axis order from the shape of the object.
            payload = json.loads(re.sub(r"```(?:json)?", "", text or "").strip())
        except (ValueError, TypeError):
            self.logger.info(f"{label} rejected response: missing or malformed JSON")
            return []
        detections = payload.get("detections", [payload]) if isinstance(payload, dict) else payload
        if not isinstance(detections, list):
            self.logger.info(f"{label} rejected response: detections must be a list")
            return []
        if not detections:
            self.logger.info(f"{label} model returned no detections")
        for det in detections:
            if not isinstance(det, dict):
                self.logger.info(f"{label} rejected detection: expected object, got {det!r}")
                continue
            if all(k in det for k in ("x_min", "y_min", "x_max", "y_max")):
                box = [det["y_min"], det["x_min"], det["y_max"], det["x_max"]]
            elif "box_2d" in det:
                box = det["box_2d"]  # legacy skill contract: y,x,y,x
            elif "bbox_2d" in det and det.get("coordinate_order") in ("xyxy", "yxyx"):
                box = det["bbox_2d"]
                if det["coordinate_order"] == "xyxy" and isinstance(box, list) and len(box) == 4:
                    box = [box[1], box[0], box[3], box[2]]
            else:
                self.logger.info(
                    f"{label} rejected detection: missing named coordinates or ambiguous box order: {det!r}"
                )
                continue
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
