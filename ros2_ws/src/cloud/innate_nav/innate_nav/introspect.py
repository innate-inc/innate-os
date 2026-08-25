#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the policy is looking at, for the operator view.

A plan says WHICH observations it was made from (Plan.history_stamps -- the
robot's own clock, assigned when each frame went up) and at WHAT SIZE the model
saw each one (Plan.history_sizes), but only the robot still has the frames.
This keeps them so the window behind any plan can be shown as the strip of
images the model actually saw, each at its own resolution.

The sizes are the point. The token budget is spent on the recent end of the
window, so the newest frame arrives near native and the oldest as a few dozen
pixels; a strip of uniform thumbnails hides that entirely and tells an operator
the model has detail it was never given.

Frames are kept as the JPEG bytes that went up -- no decode on the hot path, so
a run costs nothing until a plan asks for a window.
"""

from __future__ import annotations

from collections import OrderedDict

import cv2
import numpy as np

# Size to fall back to when the server does not report what it saw.
THUMB_W, THUMB_H = 96, 56
GAP = 3
# ~30 KB a frame at 5 fps, so this holds roughly the last four minutes. Evicting
# the oldest only loses the left end of a strip already showing a long episode.
MAX_BYTES = 32 * 1024 * 1024


class ObservationStrip:
    """The frames that went up, by stamp, and the strip for a given window."""

    def __init__(self, max_bytes: int = MAX_BYTES):
        self._frames: OrderedDict[float, bytes] = OrderedDict()
        self._bytes = 0
        self._max_bytes = max_bytes

    def __len__(self) -> int:
        return len(self._frames)

    def clear(self) -> None:
        self._frames.clear()
        self._bytes = 0

    def remember(self, jpeg: bytes, stamp: float) -> None:
        self._frames[stamp] = jpeg
        self._bytes += len(jpeg)
        while self._bytes > self._max_bytes and self._frames:
            self._bytes -= len(self._frames.popitem(last=False)[1])

    def _cell(self, stamp: float, size: tuple[int, int]) -> np.ndarray:
        """One frame at the size the model saw it. A frame that is gone
        (evicted, or sent before this run) becomes a dark cell rather than
        being skipped: the Nth cell must stay the window's Nth frame, or the
        picture lies about which observation is where."""
        w, h = max(1, size[0]), max(1, size[1])
        jpeg = self._frames.get(stamp)
        img = None if jpeg is None else cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return np.full((h, w, 3), 32, np.uint8)
        return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)

    def strip(self, stamps: list[float],
              sizes: list[list[int]] | None = None) -> np.ndarray | None:
        """The window as one filmstrip, oldest to newest, each frame at the size
        the model saw it.

        One row, not a wrapped grid: the frames differ in size by a factor of
        four, and only a single row keeps that comparable at a glance. The strip
        is wider than any panel, which is what a scroll container is for.

        `sizes` are the per-frame pixel sizes, aligned with `stamps`; without
        them every frame falls back to a thumbnail, which is what an older
        server leaves us with.
        """
        if not stamps:
            return None
        pairs = list(sizes or [])
        cells = [self._cell(s, tuple(pairs[i]) if i < len(pairs) else (THUMB_W, THUMB_H))
                 for i, s in enumerate(stamps)]
        height = max(c.shape[0] for c in cells)
        width = sum(c.shape[1] for c in cells) + GAP * (len(cells) - 1)
        out = np.zeros((height, width, 3), np.uint8)
        x = 0
        for cell in cells:
            y = (height - cell.shape[0]) // 2
            out[y : y + cell.shape[0], x : x + cell.shape[1]] = cell
            x += cell.shape[1] + GAP
        return out
