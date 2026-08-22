#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the policy is looking at, for the operator view.

A plan says WHICH observations it was made from (Plan.history_stamps -- the
robot's own clock, assigned when each frame went up), but only the robot still
has the frames. This keeps a thumbnail of every frame it sends, so the window
behind any plan can be shown as the strip of images the model actually saw.

Thumbnails, not frames: a 96x56 JPEG is ~2 KB, so a five-minute episode costs a
megabyte and the strip is a lookup rather than a round trip. Nothing here runs
unless someone is subscribed -- an operator view must not cost the robot
anything while nobody is watching.
"""

from __future__ import annotations

from collections import OrderedDict

import cv2
import numpy as np

THUMB_W, THUMB_H = 96, 56
# A five-minute episode at 5 fps is ~1500 frames; the cap is generous because
# each entry is ~2 KB, and evicting the oldest only loses the left end of a
# strip that is already showing the whole episode.
MAX_THUMBS = 2000


class ObservationStrip:
    """Thumbnails by frame stamp, and the strip for a given window."""

    def __init__(self, max_thumbs: int = MAX_THUMBS):
        self._thumbs: OrderedDict[float, np.ndarray] = OrderedDict()
        self._max = max_thumbs

    def __len__(self) -> int:
        return len(self._thumbs)

    def clear(self) -> None:
        self._thumbs.clear()

    def remember(self, jpeg: bytes, stamp: float) -> bool:
        """Decode one sent frame down to a thumbnail. False when it will not
        decode, which is the caller's cue to stop offering frames."""
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return False
        self._thumbs[stamp] = cv2.resize(img, (THUMB_W, THUMB_H), interpolation=cv2.INTER_AREA)
        while len(self._thumbs) > self._max:
            self._thumbs.popitem(last=False)
        return True

    def strip(self, stamps: list[float]) -> np.ndarray | None:
        """One row of thumbnails, oldest to newest, for the frames a plan used.

        A stamp with no thumbnail (evicted, or sent before this started
        recording) becomes a dark cell rather than being skipped: the strip's
        Nth cell must stay the window's Nth frame, or the picture lies about
        which observation is where.
        """
        if not stamps:
            return None
        cells = []
        for stamp in stamps:
            thumb = self._thumbs.get(stamp)
            if thumb is None:
                thumb = np.full((THUMB_H, THUMB_W, 3), 32, np.uint8)
            cells.append(thumb)
            cells.append(np.zeros((THUMB_H, 2, 3), np.uint8))   # hairline gap
        return np.hstack(cells[:-1])
