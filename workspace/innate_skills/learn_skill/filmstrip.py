# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The filmstrip of a trial: camera frames sampled while a draft runs, tiled into one contact sheet
that the brain reads like a strip of photos."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from innate import Image

SAMPLE_S = 0.5
FRAMES = 8  # on the sheet: the start, the end, and evenly spaced moments between
RAW_CAP = 240  # two minutes at SAMPLE_S; past it every other frame is dropped, so a long trial still fits
COLUMNS = 4
CELL = (320, 240)
INSET = (106, 80)  # the wrist camera in the cell's corner
CAPTION_PX = 22


@dataclass(frozen=True)
class Frame:
    at: float
    head: bytes | None
    wrist: bytes | None


class Filmstrip:
    """Samples two camera readers in a thread while :meth:`recording`; :meth:`sheet` is the result."""

    def __init__(self, head: Callable[[], Image | None], wrist: Callable[[], Image | None]):
        self._head = head
        self._wrist = wrist
        self._frames: list[Frame] = []
        self._stop = threading.Event()
        self._started = 0.0

    @contextmanager
    def recording(self) -> Iterator[None]:
        self._started = time.monotonic()
        thread = threading.Thread(target=self._run, name="filmstrip", daemon=True)
        thread.start()
        try:
            yield
        finally:
            self._stop.set()
            thread.join(timeout=2.0)
            self._snap()

    def sheet(self) -> bytes | None:
        """The contact sheet as JPEG, or None when the head camera never gave a frame."""
        frames = [frame for frame in self._frames if frame.head is not None]
        if not frames:
            return None
        picks = _spread(frames, FRAMES)
        cells = [_cell(frame, i, len(picks)) for i, frame in enumerate(picks)]
        blank = np.zeros_like(cells[0])
        rows = [cells[i : i + COLUMNS] for i in range(0, len(cells), COLUMNS)]
        grid = np.vstack([np.hstack(row + [blank] * (COLUMNS - len(row))) for row in rows])
        ok, encoded = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return encoded.tobytes() if ok else None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._snap()
            self._stop.wait(SAMPLE_S)

    def _snap(self) -> None:
        head, wrist = self._head(), self._wrist()
        at = time.monotonic() - self._started
        self._frames.append(Frame(at, head.jpeg if head else None, wrist.jpeg if wrist else None))
        if len(self._frames) > RAW_CAP:
            self._frames = self._frames[::2]


def _spread(frames: list[Frame], count: int) -> list[Frame]:
    if len(frames) <= count:
        return frames
    last = len(frames) - 1
    return [frames[round(i * last / (count - 1))] for i in range(count)]


def _cell(frame: Frame, index: int, count: int) -> np.ndarray:
    image = _decode(frame.head, CELL)
    if frame.wrist is not None:
        inset = _decode(frame.wrist, INSET)
        image[-INSET[1] :, -INSET[0] :] = inset
    bar = np.zeros((CAPTION_PX, CELL[0], 3), np.uint8)
    label = f"t+{frame.at:.1f}s" + (" start" if index == 0 else " end" if index == count - 1 else "")
    cv2.putText(bar, label, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return np.vstack([image, bar])


def _decode(jpeg: bytes | None, size: tuple[int, int]) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) if jpeg else None
    if image is None:
        return np.zeros((size[1], size[0], 3), np.uint8)
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)
