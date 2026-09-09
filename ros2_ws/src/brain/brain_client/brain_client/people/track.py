# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ByteTrack-style person tracking: who in this frame is who in the last one.

Two association passes (high-score detections first, then the low-score ones
that keep a blurred person attached), constant-velocity prediction between
frames, and a long lost-track memory: a track that leaves the frame keeps its
tag and its body-embedding buffer for ``track_memory_sec`` and is re-associated
when a matching person walks back in. Tags are the ``P<n>`` the model sees —
monotonic, never reused, stable across a lost-and-recovered gap; the counter
starts where the last run left it, because a respawned node's snapshot reaches
skills that are still holding the old tags.

PURE module: numpy for the embedding cosine, no ROS, no cv2.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from brain_client.people.quality import EgoMotion

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brain_client.people.types import Box, Detection

_MAX_BODY_TEMPLATES = 8  # the re-association buffer; a few poses of today's outfit


def iou(a: Box, b: Box) -> float:
    ay0, ax0, ay1, ax1 = a
    by0, bx0, by1, bx1 = b
    iw = min(ax1, bx1) - max(ax0, bx0)
    ih = min(ay1, by1) - max(ay0, by0)
    if iw <= 0.0 or ih <= 0.0:
        return 0.0
    inter = iw * ih
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0.0 else 0.0


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine of two L2-normalized embeddings; 0 when either is empty."""
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b))


def _shift(box: Box, dx: float, dy: float) -> Box:
    ymin, xmin, ymax, xmax = box
    return (ymin + dy, xmin + dx, ymax + dy, xmax + dx)


@dataclass(eq=False)
class Track:
    """One tracked person's geometry. Identity lives in the resolver, keyed by tag."""

    tag: str
    box: Box
    score: float
    first_seen: float
    last_seen: float
    head_box: Box | None = None
    velocity: tuple[float, float] = (0.0, 0.0)  # normalized box-centre units per second
    hits: int = 1
    lost: bool = False
    lost_since: float | None = None
    source: str = "body"
    body_model: str = ""
    body_templates: deque[np.ndarray] = field(default_factory=lambda: deque(maxlen=_MAX_BODY_TEMPLATES))

    @property
    def age_sec(self) -> float:
        return self.last_seen - self.first_seen

    def predicted(self, now: float) -> Box:
        dt = max(0.0, now - self.last_seen)
        return _shift(self.box, self.velocity[0] * dt, self.velocity[1] * dt)


@dataclass(frozen=True)
class TrackerConfig:
    high_score: float = 0.5
    low_score: float = 0.1
    iou_high: float = 0.3
    iou_low: float = 0.15
    # Ego-motion shifts every box between frames; the gate opens rather than
    # fragmenting the track (RFC 4.3), and the engine makes no decisions anyway.
    iou_moving_factor: float = 0.5
    max_miss_sec: float = 1.0  # unmatched for longer than this and the track is lost
    track_memory_sec: float = 300.0
    reid_cosine: float = 0.65
    max_speed: float = 1.5  # normalized units/s; a swap must not fling the prediction away
    velocity_gain: float = 0.5  # EMA on the measured velocity


class Tracker:
    """Owns the live and lost track sets and the tag counter."""

    def __init__(self, config: TrackerConfig | None = None, *, first_tag: int = 1) -> None:
        self._config = config or TrackerConfig()
        self._tracks: dict[str, Track] = {}
        self._next_tag = max(1, first_tag)
        self._recovered: list[str] = []

    @property
    def config(self) -> TrackerConfig:
        return self._config

    @property
    def next_tag(self) -> int:
        """The number the next ``P<n>`` will carry. The node persists it: the
        latched snapshot outlives a respawn, and a tag reissued to somebody else
        would let a running skill act on the wrong person (RFC 5.3.7)."""
        return self._next_tag

    def get(self, tag: str) -> Track | None:
        return self._tracks.get(tag)

    def live(self) -> list[Track]:
        return [t for t in self._tracks.values() if not t.lost]

    def lost(self) -> list[Track]:
        return [t for t in self._tracks.values() if t.lost]

    def all_tracks(self) -> list[Track]:
        return list(self._tracks.values())

    def forget(self, tag: str) -> None:
        self._tracks.pop(tag, None)

    def take_recovered(self) -> list[str]:
        """Tags that came back from the lost set this tick — the resolver resumes
        them at ``possible`` rather than trusting the old confirmation."""
        recovered, self._recovered = self._recovered, []
        return recovered

    def update(self, detections: Sequence[Detection], now: float, ego: EgoMotion | None = None) -> list[Track]:
        ego = ego or EgoMotion.still_at(now)
        loosen = 1.0 if ego.still else self._config.iou_moving_factor
        candidates = [t for t in self._tracks.values() if not t.lost or self._recoverable(t, now)]
        predictions = {t.tag: t.predicted(now) for t in candidates}

        high = [d for d in detections if d.score >= self._config.high_score]
        low = [d for d in detections if self._config.low_score <= d.score < self._config.high_score]

        taken, pending = self._associate(candidates, high, predictions, self._config.iou_high * loosen, now)
        # The low-score pass exists to hold a tracked box through a blur, so it
        # only sees live tracks: a 0.1-0.5 false positive must not revive a lost
        # tag and hand a stranger the identity that went with it.
        live_pending = [t for t in pending if not t.lost]
        self._associate(live_pending, low, predictions, self._config.iou_low * loosen, now)

        for index, detection in enumerate(high):
            if index not in taken:
                self._spawn(detection, now)

        self._age_out(now)
        return self.live()

    def _recoverable(self, track: Track, now: float) -> bool:
        """A lost track still worth an IoU pass: it may have blinked out behind
        an occluder rather than left."""
        return track.lost_since is not None and now - track.lost_since <= self._config.max_miss_sec * 2.0

    def _associate(
        self,
        tracks: Sequence[Track],
        detections: Sequence[Detection],
        predictions: dict[str, Box],
        threshold: float,
        now: float,
    ) -> tuple[set[int], list[Track]]:
        """Greedy best-IoU-first assignment, returning the detection indices
        taken and the tracks still unmatched. Greedy rather than Hungarian:
        with at most a handful of people the optimum and the greedy pick agree,
        and the result stays deterministic without a scipy dependency."""
        pairs: list[tuple[float, int, int]] = []
        for ti, track in enumerate(tracks):
            for di, detection in enumerate(detections):
                overlap = iou(predictions[track.tag], detection.box)
                if overlap >= threshold:
                    pairs.append((overlap, ti, di))
        pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
        used_tracks: set[int] = set()
        used_dets: set[int] = set()
        for _overlap, ti, di in pairs:
            if ti in used_tracks or di in used_dets:
                continue
            used_tracks.add(ti)
            used_dets.add(di)
            self._absorb(tracks[ti], detections[di], now)
        return used_dets, [t for ti, t in enumerate(tracks) if ti not in used_tracks]

    def _absorb(self, track: Track, detection: Detection, now: float) -> None:
        dt = now - track.last_seen
        if dt > 1e-3:
            ox, oy = _center(track.box)
            nx, ny = _center(detection.box)
            measured = ((nx - ox) / dt, (ny - oy) / dt)
            gain = self._config.velocity_gain
            speed = float(np.hypot(*measured))
            if speed > self._config.max_speed:
                measured = (measured[0] * self._config.max_speed / speed, measured[1] * self._config.max_speed / speed)
            track.velocity = (
                track.velocity[0] * (1 - gain) + measured[0] * gain,
                track.velocity[1] * (1 - gain) + measured[1] * gain,
            )
        if track.lost:
            track.lost = False
            track.lost_since = None
            self._recovered.append(track.tag)
        track.box = detection.box
        track.head_box = detection.head_box or track.head_box
        track.score = detection.score
        track.source = detection.source
        track.last_seen = now
        track.hits += 1

    def _spawn(self, detection: Detection, now: float) -> Track:
        tag = f"P{self._next_tag}"
        self._next_tag += 1
        track = Track(
            tag=tag,
            box=detection.box,
            score=detection.score,
            first_seen=now,
            last_seen=now,
            head_box=detection.head_box,
            source=detection.source,
        )
        self._tracks[tag] = track
        return track

    def _age_out(self, now: float) -> None:
        for track in list(self._tracks.values()):
            if not track.lost and now - track.last_seen > self._config.max_miss_sec:
                track.lost = True
                track.lost_since = now
            if track.lost_since is not None and now - track.lost_since > self._config.track_memory_sec:
                del self._tracks[track.tag]

    def note_body(self, tag: str, embedding: np.ndarray, model: str) -> None:
        track = self._tracks.get(tag)
        if track is None or embedding.size == 0:
            return
        if track.body_model and track.body_model != model:
            track.body_templates.clear()  # embeddings of different spaces are never compared
        track.body_model = model
        track.body_templates.append(embedding)

    def reassociate(self, tag: str, embedding: np.ndarray, model: str, now: float) -> str | None:
        """Fold a freshly spawned track into the lost track whose outfit it
        matches, restoring that track's tag. Returns the restored tag, or None
        when nobody matches and the new track keeps its own.

        The caller learns the tag from the return value and resumes its identity
        on this tick, so the recovery is not also queued for
        :meth:`take_recovered` — resuming twice would demote it again.
        """
        track = self._tracks.get(tag)
        if track is None or embedding.size == 0:
            return None
        best_tag, best_score = None, self._config.reid_cosine
        for other in self._tracks.values():
            if other.tag == tag or not other.lost or other.body_model != model:
                continue
            score = max((cosine(embedding, t) for t in other.body_templates), default=0.0)
            if score >= best_score:
                best_tag, best_score = other.tag, score
        if best_tag is None:
            self.note_body(tag, embedding, model)
            return None
        revived = self._tracks[best_tag]
        revived.box = track.box
        revived.head_box = track.head_box
        revived.score = track.score
        revived.source = track.source
        revived.last_seen = now
        revived.hits += track.hits
        revived.lost = False
        revived.lost_since = None
        revived.velocity = track.velocity
        del self._tracks[tag]
        self.note_body(best_tag, embedding, model)
        return best_tag

    def split(self, tag: str) -> str | None:
        """Retire ``tag`` and give the box it was following a fresh one.

        RFC 5.3.4: a face frame that accepts B on a track committed to A means
        the tracker swapped two people, so the pixels stop being A's — the body
        buffer goes with the old tag and both tags resolve afresh.
        """
        track = self._tracks.pop(tag, None)
        if track is None:
            return None
        new_tag = f"P{self._next_tag}"
        self._next_tag += 1
        self._tracks[new_tag] = Track(
            tag=new_tag,
            box=track.box,
            score=track.score,
            first_seen=track.last_seen,
            last_seen=track.last_seen,
            head_box=track.head_box,
            velocity=track.velocity,
            source=track.source,
        )
        return new_tag


def _center(box: Box) -> tuple[float, float]:
    ymin, xmin, ymax, xmax = box
    return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)
