# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Astra alternative to YOLOE with the same detector and tracker contracts."""
import base64
import json
import time

import cv2
import numpy as np

from innate import Llm, SkillReturn
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W, pixel_to_floor
from innate_skills.approach import settled_frame
from innate_skills.pick_up_pile_little_toy import PickUpPileLittleToy, encode_jpeg
from innate_skills.yoloe_pickup_base import detection_candidates

DETECTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['box'],
    'properties': {'box': {'anyOf': [
        {'type': 'null'},
        {'type': 'array', 'items': {'type': 'number'}, 'minItems': 4, 'maxItems': 4},
    ]}},
}


def parse_candidates(text, width, height):
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != {'box'}:
        raise ValueError('Expected one box or null')
    box = result['box']
    detections = []
    if box is not None:
        if (not isinstance(box, list) or len(box) != 4 or
                any(type(v) not in (int, float) for v in box)):
            raise ValueError('Expected four numeric box coordinates')
        x1, y1, x2, y2 = box
        detections = [{'bbox_xyxy': box, 'grasp_point_xy': [(x1+x2)/2, (y1+y2)/2],
                       'confidence': 1.0}]
    # Preserve the downstream YOLOE contract locally. Confidence is a synthetic
    # adapter value, not a model probability; the grasp point is the box center.
    payload = {'image': {'width': width, 'height': height}, 'detections': detections}
    return detection_candidates(payload), payload


class PickUpPileLittleToyAstra(PickUpPileLittleToy):
    """Pick the closest visible individual small soft toy on the floor using Astra Ultrafast."""

    requires_llm = True
    llm: Llm = Llm(
        'openai:gpt-6-astra', thinking='low',
        extra_body=json.dumps({
            'service_tier': 'ultrafast',
            'text': {'format': {'type': 'json_schema', 'name': 'toy_detections',
                                'schema': DETECTION_SCHEMA, 'strict': True}},
        }),
    )

    def execute(self) -> SkillReturn:
        """Locate the nearest floor toy with Astra, track it, then pick it up."""
        return super().execute()

    def _detect_candidates(self, prompt):
        started = time.monotonic()
        self.mobility.stop()
        frame = settled_frame(self, self._p['settle_s'], read=lambda: self.main_highres_image)
        self.check_cancelled()
        if not frame:
            raise SkillFailed('No high-resolution camera image for Astra toy pickup')
        try:
            image = cv2.imdecode(np.frombuffer(base64.b64decode(frame, validate=True), np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Invalid toy camera JPEG')
            height, width = image.shape[:2]
            question = (
                f'{width}x{height} image. Box the closest individual small toy on the floor. '
                'Exclude baskets and held objects. Pixels [left,top,right,bottom]; null if absent.'
            )
            request_started = time.monotonic()
            text = self.llm.ask(frame, question, logger=self.logger, retries=1)
            self.check_cancelled()
            request_seconds = time.monotonic() - request_started
            if not text:
                raise ValueError('Astra returned no detection response')
            candidates, payload = parse_candidates(text, width, height)
            tracking_image = base64.b64encode(encode_jpeg(cv2.resize(image, (IMG_W, IMG_H)))).decode()
        except (OSError, ValueError, KeyError, TypeError, cv2.error) as error:
            raise SkillFailed(f'Astra toy detection failed: {error}') from error
        candidates = self._box_exclusion.filter(candidates, tracking_image)
        candidates = [c for c in candidates if pixel_to_floor(c[0], c[1], self._p['tilt_deg']) is not None]
        self._astra_payload = payload
        self._astra_timing = {'request_seconds': request_seconds, 'total_seconds': time.monotonic() - started}
        self.logger.info(f'[AstraToyPickup] model=gpt-6-astra requested_tier=ultrafast '
                         f'timing={self._astra_timing} candidates={len(candidates)} detections={payload}')
        return candidates, tracking_image
