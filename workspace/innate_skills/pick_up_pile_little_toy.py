# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Select one small colorful soft toy inside a floor pile, then track and lift it."""
import base64
import math

import cv2
import numpy as np

from innate import MainHighResImage, SkillReturn
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W, pixel_to_floor
from innate_skills.approach import settled_frame
from innate_skills.pick_any_object import PickAnyObject
from innate_skills.pick_sock_pile_yoloe import PickSockPileYoloe
from innate_skills.yoloe_pickup_base import detection_candidates, request_yoloe


def crop_window(box, width, height):
    """Add half a pile width/height on each side; clamp at camera edges."""
    x1, y1, x2, y2 = box
    dx, dy = (x2 - x1) / 2, (y2 - y1) / 2
    return (max(0, math.floor(x1 - dx)), max(0, math.floor(y1 - dy)),
            min(width, math.ceil(x2 + dx)), min(height, math.ceil(y2 + dy)))


def individual_candidates(payload, crop, pile_box, image_size):
    """Reject pile-sized boxes and map crop mask points into controller pixels."""
    left, top, right, bottom = crop
    cw, ch = right - left, bottom - top
    if payload['image'] != {'width': cw, 'height': ch}:
        raise ValueError('Detector crop dimensions differ from submitted image')
    width, height = image_size
    px1, py1, px2, py2 = pile_box
    pile_area = (px2 - px1) * (py2 - py1)
    result = []
    for u, v, grip, box in detection_candidates(payload):
        x1, y1, x2, y2 = box
        x1, x2 = left + x1 * cw / IMG_W, left + x2 * cw / IMG_W
        y1, y2 = top + y1 * ch / IMG_H, top + y2 * ch / IMG_H
        u, v = left + u * cw / IMG_W, top + v * ch / IMG_H
        if not (px1 <= u <= px2 and py1 <= v <= py2):
            continue
        # The observed aggregate detection spans the entire pile. Never use its
        # center as a substitute for an individually localized toy.
        if (x2 - x1) * (y2 - y1) >= 0.5 * pile_area:
            continue
        result.append((u * IMG_W / width, v * IMG_H / height, grip,
                       (x1 * IMG_W / width, y1 * IMG_H / height,
                        x2 * IMG_W / width, y2 * IMG_H / height)))
    return result


def encode_jpeg(image):
    ok, data = cv2.imencode('.jpg', image)
    if not ok:
        raise ValueError('Cannot encode toy detector image')
    return data.tobytes()


class PickUpPileLittleToy(PickSockPileYoloe):
    """Pick one small soft toy from a colorful toy pile on the floor.

    Uses a pile crop to select an individual toy mask, then the existing floor
    approach and grip controller. Requires physical rehearsal for these toys;
    finger spacing cannot prove that exactly one toy is held.
    """

    main_highres_image: MainHighResImage | None
    detection_prompt = 'a small soft toy from the pile'
    pile_prompts = ('a pile of small toys',)
    toy_prompts = ('pink toy', 'orange toy', 'green toy', 'a small stuffed animal')

    def execute(self) -> SkillReturn:
        """Pick one localized toy from the floor pile and hold it for the operator."""
        return super().execute()

    def _detect_candidates(self, prompt):
        self.mobility.stop()
        frame = settled_frame(self, self._p['settle_s'], read=lambda: self.main_highres_image)
        self.check_cancelled()
        if not frame:
            raise SkillFailed('No high-resolution camera image for toy pickup')
        try:
            jpeg = base64.b64decode(frame, validate=True)
            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Invalid toy camera JPEG')
            height, width = image.shape[:2]
            tracking_image = base64.b64encode(encode_jpeg(cv2.resize(image, (IMG_W, IMG_H)))).decode()
            pile_payload = request_yoloe(jpeg, self.pile_prompts)
            self.check_cancelled()
            if pile_payload['image'] != {'width': width, 'height': height}:
                raise ValueError('Detector dimensions differ from camera image')
            piles = detection_candidates(pile_payload)
            candidates = []
            # Each crop comes from this same immutable camera frame. Candidate
            # order is pile confidence, then individual confidence within it.
            for _, _, _, box in piles:
                self.check_cancelled()
                pile_box = (box[0] * width / IMG_W, box[1] * height / IMG_H,
                            box[2] * width / IMG_W, box[3] * height / IMG_H)
                crop = crop_window(pile_box, width, height)
                left, top, right, bottom = crop
                payload = request_yoloe(encode_jpeg(image[top:bottom, left:right]), self.toy_prompts)
                self.check_cancelled()
                candidates.extend(individual_candidates(payload, crop, pile_box, (width, height)))
        except (OSError, ValueError, KeyError, TypeError, cv2.error) as error:
            raise SkillFailed(f'Toy detection failed: {error}') from error
        candidates = self._box_exclusion.filter(candidates, tracking_image)
        self.logger.info(f'[ToyPickup] piles={len(piles)} individual_candidates={len(candidates)}')
        return candidates, tracking_image

    def _choose_cand(self, candidates):
        # The sock-pile variant reselects by confidence. For a single toy, use
        # the existing instance-memory gate instead of switching on re-detection.
        floor_candidates = [c for c in candidates if pixel_to_floor(c[0], c[1], self._p['tilt_deg']) is not None]
        return PickAnyObject._choose_cand(self, floor_candidates) if floor_candidates else None
