# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Astra alternative to YOLOE with the same detector and tracker contracts."""
import base64
import json
import time

import cv2
import numpy as np

from innate import Llm, SkillReturn, Waypoint
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W, pixel_to_floor
from innate_skills.approach import FloorApproach, settled_frame
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


def visual_grip_held(host):
    host.mobility.stop()
    stale = host.wrist_image
    def fresh_frame():
        image = host.wrist_image
        return image if image and image is not stale else None
    image = host.wait_for(fresh_frame, timeout=1.0)
    host.check_cancelled()
    if not image:
        raise SkillFailed('Grasp verification unknown: no fresh wrist image; preserving grip')
    started = time.monotonic()
    answer = host._grip_llm.ask(
        image,
        'Is a keychain in a clear plastic sleeve held by the gripper, pinched between the fingers or '
        'hanging from them? It is often only partly visible, cut off at an edge or in a corner of the '
        'photo, and the sleeve is thin and transparent, so count any part of it touching the fingers, '
        'even just a glint, edge or crease of clear plastic. Keychains lying on the floor below are not '
        'held. Return only true or false.',
        logger=host.logger, retries=1,
    )
    host.check_cancelled()
    answer = answer.strip().lower() if isinstance(answer, str) else None
    host.logger.info(f'[AstraGrip] answer={answer!r} seconds={time.monotonic()-started:.3f}')
    if answer not in ('true', 'false'):
        # Do not clear _holding: the normal failure cleanup keeps the grip.
        raise SkillFailed('Grasp verification unknown: invalid or failed Astra response; preserving grip')
    return answer == 'true'


class PickUpSleevedObjectAstra(PickUpPileLittleToy):
    """Pick a sleeved keychain off the floor, from the middle of the group when several lie together, using Astra Ultrafast."""

    detection_prompt = 'the sleeved keychains on the floor'
    # Each run starts where the last handoff left it, facing the wall, so look behind first.
    # Any keychain will do, and a floor point 1.5 m out moves ~18 cm per degree of
    # head sway, so use the range-scaled gate that re-anchors instead of the sock pile's 12 cm lock.
    _p = {
        **PickUpPileLittleToy._p,
        'search_turns_deg': (180, 90, 90, 90),
        'lock_target_identity': False,
        'mem_gate_m': 0.35,
        'mem_gate_frac': 0.4,
        # Come in above the bowl's rim, then drop straight down onto the keychain and lift clear of it.
        'hover_z': 0.26,
        'hover_s': 0.8,
        'descent_s': 1.0,
        'lift_rad': 0.9,
        'grasp_attempts': 4,
    }
    # The handoff then spins in place, swinging the tail ~25 cm round over the keychains just picked from.
    BACK_OFF_M = 0.15
    requires_llm = True
    llm: Llm = Llm(
        'openai:gpt-6-astra', thinking='low',
        extra_body=json.dumps({
            'service_tier': 'ultrafast',
            'text': {'format': {'type': 'json_schema', 'name': 'sleeved_object',
                                'schema': DETECTION_SCHEMA, 'strict': True}},
        }),
    )

    # Separate from the detector's box schema; this call returns one boolean.
    _grip_llm = Llm('openai:gpt-6-astra', thinking='low',
                    extra_body=json.dumps({'service_tier': 'ultrafast', 'max_output_tokens': 256}))

    def _object_held(self):
        return visual_grip_held(self)

    def execute(self) -> SkillReturn:
        """Find the sleeved keychains on the floor with Astra, then pick one, from the middle of the group if they lie together."""
        result = super().execute()
        if not FloorApproach(self, self._p, self._detect_px).drive(-self.BACK_OFF_M):
            self.logger.warning("[AstraSleevedPickup] could not back away from the keychains; holding position")
        return result

    def _approach_grasp(self, x, y, z):
        # Folded, the claw sits ~7 cm off the floor in front of the base; a direct sweep from there
        # crosses the bowl's near rim low, so rise close to the robot first.
        pitch = self._p['arm_pitch']
        rise = next(
            (p for p in ((0.16, 0.077, 0.28), (0.18, 0.077, 0.28), (0.20, 0.0, 0.30))
             if self.manipulation.reachable(*p, pitch=pitch)),
            None,
        )
        if rise is not None:
            self.manipulation.follow([Waypoint(*rise, pitch=pitch, duration=0.7)])
            self.check_cancelled()
        return super()._approach_grasp(x, y, z)

    def _detect_candidates(self, prompt):
        started = time.monotonic()
        self.mobility.stop()
        frame = settled_frame(self, self._p['settle_s'], read=lambda: self.main_highres_image)
        self.check_cancelled()
        if not frame:
            raise SkillFailed('No high-resolution camera image for Astra sleeved-object pickup')
        try:
            image = cv2.imdecode(np.frombuffer(base64.b64decode(frame, validate=True), np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Invalid sleeved-object camera JPEG')
            height, width = image.shape[:2]
            question = (
                f'{width}x{height} image. Box the keychains in plastic sleeves lying on the floor ahead: if several '
                'lie together, one box around that whole group; if they are spread apart, only the closest one. '
                'Exclude held objects. Pixels [left,top,right,bottom]; null if absent.'
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
            raise SkillFailed(f'Astra sleeved-object detection failed: {error}') from error
        candidates = self._box_exclusion.filter(candidates, tracking_image)
        candidates = [c for c in candidates if pixel_to_floor(c[0], c[1], self._p['tilt_deg']) is not None]
        self._astra_payload = payload
        self._astra_timing = {'request_seconds': request_seconds, 'total_seconds': time.monotonic() - started}
        self.logger.info(f'[AstraSleevedPickup] model=gpt-6-astra requested_tier=ultrafast '
                         f'timing={self._astra_timing} candidates={len(candidates)} detections={payload}')
        return candidates, tracking_image
