# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Comparison pickup: Qwen generates pixels directly, without YOLOE or masks."""
import base64
import json
import time
import uuid
from pathlib import Path

from innate import Llm, SkillReturn
from innate.exceptions import SkillFailed
from innate.geometry import IMG_W, IMG_H, pixel_to_floor
from innate_llm import Image, Message, Request, Role, Text, Thinking
from innate_skills.approach import settled_frame
from innate_skills.pick_sock_fast import PickSockFast
from innate_skills.pick_sock_pile_yoloe import PickSockPileYoloe
from innate_skills.sock_contact import error_details


def parse_pixel(text, width=IMG_W, height=IMG_H):
    result = json.loads(text)
    if not isinstance(result, dict) or type(result.get('found')) is not bool:
        raise ValueError('Expected found boolean')
    if not result['found']:
        return None
    x, y = result.get('x'), result.get('y')
    if type(x) is not int or type(y) is not int or not (0 <= x <= 1000 and 0 <= y <= 1000):
        raise ValueError('Expected integer coordinates normalized to 0-1000')
    return min(width-1, x*width/1000), min(height-1, y*height/1000)


class PickSockPileQwenPixels(PickSockPileYoloe):
    """Pick a floor pile using a direct Qwen grasp pixel; no YOLOE detection."""

    requires_llm = True
    llm: Llm = Llm('openai-chat:Qwen3.8-Flash-Next', thinking='minimal', extra_body=json.dumps({
        'chat_template_kwargs': {'enable_thinking': False},
        'response_format': {'type': 'json_schema', 'json_schema': {
            'name': 'grasp_pixel', 'strict': True, 'schema': {
                'type': 'object', 'properties': {
                    'found': {'type': 'boolean'},
                    'x': {'type': 'integer', 'minimum': 0, 'maximum': 1000},
                    'y': {'type': 'integer', 'minimum': 0, 'maximum': 1000}},
                'required': ['found', 'x', 'y'], 'additionalProperties': False}}}}))

    def execute(self) -> SkillReturn:
        """Compare direct Qwen pixels: pick floor socks, preferably at a fabric contact between socks."""
        return super().execute()

    def _detect_px(self, prompt):
        # Reuse overlay + sighting + tracker initialization, without starting
        # the parent pile skill's separate numbered-candidate Qwen request.
        return PickSockFast._detect_px(self, prompt)

    def _detect_candidates(self, prompt):
        self.mobility.stop()
        image = settled_frame(self, self._p['settle_s'])
        self.check_cancelled()
        if not image:
            raise SkillFailed('No camera image for Qwen')
        self.overlay.readout('Qwen choosing a fabric grasp point', busy=True)
        question = (
            'Choose one grasp point on visible sock fabric in a floor pile, outside and away from any box. '
            'Prefer where TWO socks touch. Never point at gaps, bare floor, furniture, or the robot. '
            'Return {"found":true,"x":integer,"y":integer} with coordinates normalized to 0-1000, '
            'x from left and y from top. If no floor socks: {"found":false,"x":0,"y":0}.'
        )
        request_id = uuid.uuid4().hex[:8]
        started = time.monotonic()
        self.logger.info(f'[QwenPixel] request_started id={request_id} image={IMG_W}x{IMG_H}')
        try:
            provider = self.llm._provider()
            if provider is None:
                raise ValueError('No configured Qwen provider')
            reply = provider.run(Request(system='', messages=(Message(Role.USER, (
                Text(question), Image(base64.b64decode(image)))),), temperature=0,
                thinking=Thinking.MINIMAL, max_tokens=48), timeout=5)
            self.check_cancelled()
            pixel = parse_pixel(reply.message.text())
        except (ValueError, TypeError, KeyError) as exc:
            self.logger.info('[QwenPixel] rejected ' + json.dumps(dict(request_id=request_id, **error_details(exc))))
            self.overlay.readout('Qwen returned no usable grasp pixel')
            return [], image
        except Exception as exc:
            # Cancellation must propagate; never convert it into another search.
            self.check_cancelled()
            self.logger.info('[QwenPixel] request_failed ' + json.dumps(dict(request_id=request_id, **error_details(exc))))
            self.overlay.readout('Qwen request failed')
            raise SkillFailed('Qwen grasp pixel request failed') from exc
        elapsed = time.monotonic()-started
        row = dict(request_id=request_id, elapsed_s=round(elapsed, 3), pixel=pixel,
                   prompt_tokens=reply.usage.prompt, output_tokens=reply.usage.output)
        self.logger.info('[QwenPixel] response ' + json.dumps(row))
        try:
            audit = Path('/tmp/sock-qwen-pixel-audit')
            audit.mkdir(exist_ok=True)
            stem = audit / f'{time.time_ns()}-{request_id}'
            stem.with_suffix('.jpg').write_bytes(base64.b64decode(image))
            stem.with_suffix('.json').write_text(json.dumps(row))
        except OSError:
            self.logger.info('[QwenPixel] image audit unavailable')
        if pixel is None:
            return [], image
        x, y = pixel
        # A local feature window for optical flow, NOT a model-predicted box.
        box = (max(0,x-40), max(0,y-40), min(IMG_W,x+40), min(IMG_H,y+40))
        candidates = self._box_exclusion.filter([(x,y,None,box)], image)
        self.overlay.readout('Qwen grasp pixel selected' if candidates else 'Qwen pixel is inside the box')
        return candidates, image

    def _choose_cand(self, candidates):
        self._coasts = 0
        for candidate in candidates:
            floor = pixel_to_floor(candidate[0], candidate[1], self._p['tilt_deg'])
            self.logger.info('[QwenPixel] selection ' + json.dumps({
                'pixel': candidate[:2], 'floor_xy': floor,
                'reason': 'selected' if floor is not None else 'no_floor_projection'}))
            if floor is not None:
                return candidate
        return None

    def _draw_sighting(self, px, box, dist, n):
        self.overlay.clear('track', 'steer', 'target')
        self.overlay.reticle('target', px, label='Qwen grasp pixel')
        self.overlay.readout('Qwen grasp pixel selected; tracking fabric')
