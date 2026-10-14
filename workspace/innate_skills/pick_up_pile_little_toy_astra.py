# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Astra alternative to YOLOE with the same detector and tracker contracts."""
import base64
import json
import math
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
    'type': 'object', 'additionalProperties': False,
    'required': ['image', 'detections'],
    'properties': {
        'image': {'type': 'object', 'additionalProperties': False,
                  'required': ['width', 'height'],
                  'properties': {'width': {'type': 'integer'}, 'height': {'type': 'integer'}}},
        'detections': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['confidence', 'bbox_xyxy', 'grasp_point_xy'],
            'properties': {
                'confidence': {'type': 'number'},
                'bbox_xyxy': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 4, 'maxItems': 4},
                'grasp_point_xy': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 2, 'maxItems': 2},
            },
        }},
    },
}


def parse_candidates(text, width, height):
    payload = json.loads(text)
    if payload['image'] != {'width': width, 'height': height}:
        raise ValueError('Detector dimensions differ from camera image')
    # Identical native-pixel JSON contract to YOLOE. Astra estimates a grasp
    # point visually; it does not produce a segmentation mask.
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
                f'Robot head camera, {width} pixels wide by {height} pixels high. '
                'Locate the individual small soft toys lying on the floor in front of the robot, '
                'including toys in a pile. Return up to six nearest visible individual toys. '
                'Exclude objects inside baskets or boxes, objects held by the gripper, robot hardware, '
                'and background furniture. Never return a box around a whole pile. '
                'For each toy give a tight bbox_xyxy [left,top,right,bottom] and grasp_point_xy [x,y] '
                'on the visible body of that same toy, not empty space or a gap between toys. '
                'Use absolute pixels in THIS image, not 0-1000 coordinates. '
                'Confidence is from 0 to 1. Set image width and height to the supplied dimensions. '
                'Return an empty detections list if no individual floor toy is visible. '
                'Return only the specified JSON object.'
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
        projected = [(c, pixel_to_floor(c[0], c[1], self._p['tilt_deg'])) for c in candidates]
        candidates = [c for c, xy in sorted(
            ((c, xy) for c, xy in projected if xy is not None),
            key=lambda pair: math.hypot(*pair[1]),
        )]
        self._astra_payload = payload
        self._astra_timing = {'request_seconds': request_seconds, 'total_seconds': time.monotonic() - started}
        self.logger.info(f'[AstraToyPickup] model=gpt-6-astra requested_tier=ultrafast '
                         f'timing={self._astra_timing} candidates={len(candidates)} detections={payload}')
        return candidates, tracking_image
