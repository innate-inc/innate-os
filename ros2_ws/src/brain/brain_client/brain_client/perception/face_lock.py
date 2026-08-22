# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Choose one face and report when it stays centered."""

from __future__ import annotations

from dataclasses import dataclass

_LOCK_SECONDS = 1.0
_LOST_SECONDS = 1.0
_MATCH_IOU = 0.25
_MIN_FACE_HEIGHT = 0.10


@dataclass(frozen=True)
class FaceZone:
    left: float
    top: float
    right: float
    bottom: float


CENTER_ZONE = FaceZone(left=0.35, top=0.20, right=0.65, bottom=0.75)


@dataclass(frozen=True)
class FaceBox:
    center_x: float
    center_y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def centered(self) -> bool:
        return (
            CENTER_ZONE.left <= self.center_x <= CENTER_ZONE.right
            and CENTER_ZONE.top <= self.center_y <= CENTER_ZONE.bottom
        )

    @property
    def large_enough(self) -> bool:
        return self.height >= _MIN_FACE_HEIGHT

    def iou(self, other: FaceBox) -> float:
        left = max(self.center_x - self.width / 2, other.center_x - other.width / 2)
        right = min(self.center_x + self.width / 2, other.center_x + other.width / 2)
        top = max(self.center_y - self.height / 2, other.center_y - other.height / 2)
        bottom = min(self.center_y + self.height / 2, other.center_y + other.height / 2)
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        union = self.area + other.area - intersection
        return intersection / union if union > 0.0 else 0.0


@dataclass(frozen=True)
class FaceLockResult:
    face: FaceBox | None
    progress: float = 0.0
    locked: bool = False
    just_locked: bool = False


class FaceLock:
    def __init__(self) -> None:
        self._face: FaceBox | None = None
        self._last_seen: float | None = None
        self._last_observed: float | None = None
        self._centered_since: float | None = None
        self._locked = False

    def observe(self, faces: list[FaceBox], now: float) -> FaceLockResult:
        if self._last_observed is not None and now - self._last_observed >= _LOST_SECONDS:
            self._reset()
        self._last_observed = now

        face = self._match(faces, now)
        if face is None:
            return FaceLockResult(face=None)

        self._face = face
        self._last_seen = now
        if not face.centered or not face.large_enough:
            self._centered_since = None
            return FaceLockResult(face=face, locked=self._locked)

        if self._centered_since is None:
            self._centered_since = now
        progress = min(1.0, (now - self._centered_since) / _LOCK_SECONDS)
        just_locked = not self._locked and progress >= 1.0
        self._locked = self._locked or just_locked
        return FaceLockResult(face=face, progress=progress, locked=self._locked, just_locked=just_locked)

    def _match(self, faces: list[FaceBox], now: float) -> FaceBox | None:
        if self._face is None:
            return max(faces, key=lambda face: face.area, default=None)

        match = max(faces, key=self._face.iou, default=None)
        if match is not None and self._face.iou(match) >= _MATCH_IOU:
            return match

        if self._last_seen is not None and now - self._last_seen < _LOST_SECONDS:
            return None

        self._reset()
        return max(faces, key=lambda face: face.area, default=None)

    def _reset(self) -> None:
        self._face = None
        self._last_seen = None
        self._centered_since = None
        self._locked = False
