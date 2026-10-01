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
        box = (x1 * IMG_W / width, y1 * IMG_H / height, x2 * IMG_W / width, y2 * IMG_H / height)
        found.append((score, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, None, box)))
    return [candidate for _, candidate in sorted(found, key=lambda pair: pair[0], reverse=True)]


class PickSockYoloe(PickSockFast):
    """Search by turning, then approach and pick a green floor sock using YOLOE."""

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
        return super().execute("green sock")

    def _detect_candidates(self, prompt):
        self.mobility.stop()
        img = settled_frame(self, self._p["settle_s"])
        self.check_cancelled()
        if not img:
            raise SkillFailed("No camera image for YOLOE")
        boundary = uuid.uuid4().hex
        jpeg = base64.b64decode(img)
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\ngreen sock\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"frame.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode() + jpeg + f"\r\n--{boundary}--\r\n".encode()
        url = os.environ.get("SOCK_YOLOE_URL", "http://100.107.224.18:8765").rstrip("/") + "/detect?confidence=0.05"
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.load(response)
            self.check_cancelled()
            candidates = detection_candidates(payload)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SkillFailed(f"YOLOE detection failed: {error}") from error
        self.logger.info(
            f"[YOLOE] prompt=green sock elapsed={time.monotonic() - started:.3f}s "
            f"detections={len(candidates)} boxes={payload['detections']}"
        )
        return candidates, img
