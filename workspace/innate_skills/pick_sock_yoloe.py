# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Rehearsed green-sock pickup using the remote YOLOE detector."""
import base64
import json
import math
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from innate_skills.approach import settled_frame
from innate_skills.pick_sock_fast import PickSockFast

from innate import SkillReturn
from innate.exceptions import SkillFailed
from innate.geometry import IMG_H, IMG_W


def detection_candidates(payload, threshold=0.05):
    """Validate original-image pixel boxes and map them into the tracking image."""
    size = payload["image"]
    width, height = float(size["width"]), float(size["height"])
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError("Invalid detector image dimensions")
    found = []
    for item in payload["detections"]:
        score = float(item["confidence"])
        box = [float(v) for v in item["bbox_xyxy"]]
        if len(box) != 4 or not all(math.isfinite(v) for v in [score, *box]):
            raise ValueError("Invalid detector box")
        x1, y1, x2, y2 = box
        if not (0 <= score <= 1 and 0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError("Out-of-range detector box")
        if score < threshold:
            continue
        # The server picks the mask pixel nearest the box center. No center
        # fallback: an absent/empty mask cannot establish a fabric grasp point.
        point = item["grasp_point_xy"]
        if point is None:
            continue
        if len(point) != 2:
            raise ValueError("Invalid mask grasp point")
        u, v = map(float, point)
        if not (math.isfinite(u) and math.isfinite(v) and
                x1 <= u <= x2 and y1 <= v <= y2 and 0 <= u < width and 0 <= v < height):
            raise ValueError("Mask grasp point outside detection/image")
        box = (x1 * IMG_W / width, y1 * IMG_H / height, x2 * IMG_W / width, y2 * IMG_H / height)
        found.append((score, (u * IMG_W / width, v * IMG_H / height, None, box)))
    return [candidate for _, candidate in sorted(found, key=lambda pair: pair[0], reverse=True)]


class PickSockYoloe(PickSockFast):
    """Search by turning, then approach and pick a green floor sock using YOLOE."""

    detection_prompt = "green sock"
    requires_llm = False
    _p = {
        **PickSockFast._p,
        "search_turns_deg": (0, -90, -90, -90),
        "silent_search": True,
        "rot_wz_max": 1.1,
        "rot_kp": 3.0,
    }

    def execute(self) -> SkillReturn:
        """Find and pick a green sock. Turns to search automatically."""
        return super().execute(self.detection_prompt)

    def _detect_candidates(self, prompt):
        self.mobility.stop()
        img = settled_frame(self, self._p["settle_s"])
        self.check_cancelled()
        if not img:
            raise SkillFailed("No camera image for YOLOE")
        boundary = uuid.uuid4().hex
        jpeg = base64.b64decode(img)
        audit = Path('/tmp/sock-yolo-audit') / f'{time.time_ns()}-{boundary[:8]}'
        try:
            audit.parent.mkdir(exist_ok=True)
            audit.with_suffix('.jpg').write_bytes(jpeg)
        except OSError as error:
            self.logger.warning(f"[YOLOE] image audit unavailable: {error}")
        prompts = getattr(self, "detection_prompts", (self.detection_prompt,))
        prompt_fields = "".join(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n{label}\r\n"
            for label in prompts
        )
        body = (prompt_fields +
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"frame.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode() + jpeg + f"\r\n--{boundary}--\r\n".encode()
        config_path = Path(__file__).resolve().parents[1] / "config" / "sock_detector.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        # Explicit environment routing must not inherit another endpoint's credential.
        if os.environ.get("SOCK_YOLOE_URL"):
            endpoint = os.environ["SOCK_YOLOE_URL"]
            api_key = os.environ.get("SOCK_YOLOE_API_KEY", "")
        else:
            endpoint = config.get("url", "http://100.107.224.18:8765")
            api_key = config.get("api_key", "")
        url = endpoint.rstrip("/") + "/detect?confidence=0.05"
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "Mozilla/5.0"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(
            url, data=body, headers=headers
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.load(response)
            self.check_cancelled()
            candidates = detection_candidates(payload)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SkillFailed(f"YOLOE detection failed: {error}") from error
        try:
            audit.with_suffix('.json').write_text(json.dumps({'prompts': list(prompts), 'response': payload}))
        except OSError as error:
            self.logger.warning(f"[YOLOE] response audit unavailable: {error}")
        self.logger.info(f"[YOLOE] audit={audit}")
        self.logger.info(
            f"[YOLOE] prompts={prompts!r} elapsed={time.monotonic() - started:.3f}s "
            f"detections={len(candidates)} boxes={[{k: v for k, v in d.items() if k != 'mask_png_base64'} for d in payload['detections']]}"
        )
        self._yolo_payload = payload
        return candidates, img
