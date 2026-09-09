# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Who is in one frame: a person box, and a name for it when the roster has one.

The face is the only thing that can put a name on somebody. The outfit is a
same-day proxy — bound to a person the moment a face confirms them, matched
while they are turned away, expired overnight — so it carries a name, it can
never give one: a stranger in Theo's jacket reads as ``probable``, never as
Theo. Both matches take the best candidate above an accept line only when it
beats the runner-up by a margin, because the failure that matters is calling
Ana "Theo" out loud, not failing to recognize her.

Someone the roster has never seen is enrolled once three agreeing looks at
their face have arrived, which is what keeps a passer-by from becoming three
new people.

PURE module: cv2, numpy and a roster to ask, no ROS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from brain_client.people.models import crop

if TYPE_CHECKING:
    from brain_client.people.models import Box, Models
    from brain_client.people.roster import Roster

FACE_ACCEPT = 0.42
FACE_MARGIN = 0.05
FACE_AGREES = 0.38  # pairwise floor among the looks that enrol somebody new
OUTFIT_ACCEPT = 0.78
OUTFIT_MARGIN = 0.07
"""Cosine decision points for SFace and OSNet: starting points from the model
cards, to be moved once there are recordings to move them with."""

ENROL_LOOKS = 3
ENROL_WINDOW_SEC = 10.0
HEAD_FRACTION = 0.4  # the top of a person box, where the face is
MIN_FACE_PX = 24  # below this the crop carries no detail SFace can use


@dataclass(frozen=True)
class Sighting:
    """One person in the frame, and what the roster makes of them."""

    box: Box
    state: str  # "known" (a face said so) | "probable" (their outfit did) | "unknown"
    person_id: str | None = None
    name: str | None = None


class Recognizer:
    def __init__(self, models: Models, roster: Roster) -> None:
        self._models = models
        self._roster = roster
        self._pending: list[tuple[float, np.ndarray]] = []

    def look(self, frame_bgr: np.ndarray, now: float) -> list[Sighting]:
        return [self._identify(frame_bgr, box, now) for box in self._models.detector.detect(frame_bgr)]

    def _identify(self, frame_bgr: np.ndarray, box: Box, now: float) -> Sighting:
        face = self._face_vector(frame_bgr, box)
        if face is not None:
            person_id = _best(self._roster.faces(), face, FACE_ACCEPT, FACE_MARGIN) or self._enrol(face, now)
            if person_id is not None:
                self._confirm(frame_bgr, box, person_id, face, now)
                return Sighting(box, "known", person_id, self._roster.name_of(person_id))
            return Sighting(box, "unknown")
        outfit = self._outfit_vector(frame_bgr, box)
        person_id = None if outfit is None else _best(self._roster.outfits(now), outfit, OUTFIT_ACCEPT, OUTFIT_MARGIN)
        if person_id is None:
            return Sighting(box, "unknown")
        self._roster.seen(person_id, now)
        return Sighting(box, "probable", person_id, self._roster.name_of(person_id))

    def _confirm(self, frame_bgr: np.ndarray, box: Box, person_id: str, face: np.ndarray, now: float) -> None:
        """A face just vouched for this person, so today's outfit becomes
        theirs: from behind, the engine matches what the face vouched for."""
        self._roster.add_face(person_id, face, now)
        outfit = self._outfit_vector(frame_bgr, box)
        if outfit is not None:
            self._roster.set_outfit(person_id, outfit, now)

    def _enrol(self, face: np.ndarray, now: float) -> str | None:
        """A new person, once ENROL_LOOKS recent looks agree with this one.
        They must agree with each other and not merely fail to match the
        roster, or two strangers passing would enrol as one."""
        self._pending = [(stamp, vector) for stamp, vector in self._pending if now - stamp < ENROL_WINDOW_SEC]
        self._pending.append((now, face))
        agreeing = [vector for _, vector in self._pending if float(vector @ face) >= FACE_AGREES]
        if len(agreeing) < ENROL_LOOKS:
            return None
        self._pending.clear()
        return self._roster.create(agreeing, now)

    def _face_vector(self, frame_bgr: np.ndarray, box: Box) -> np.ndarray | None:
        if self._models.face_locator is None or self._models.face is None:
            return None
        head = crop(frame_bgr, (box[0], box[1], box[0] + (box[2] - box[0]) * HEAD_FRACTION, box[3]))
        hits = [hit for hit in self._models.face_locator.locate(head) if hit.h >= MIN_FACE_PX]
        if not hits:
            return None
        return self._models.face.embed(head, max(hits, key=lambda hit: hit.score))

    def _outfit_vector(self, frame_bgr: np.ndarray, box: Box) -> np.ndarray | None:
        return None if self._models.outfit is None else self._models.outfit.embed(crop(frame_bgr, box))


def _best(gallery: list[tuple[str, np.ndarray]], vector: np.ndarray, accept: float, margin: float) -> str | None:
    """The one person this vector belongs to, or None when the gallery cannot
    say: nobody above ``accept``, or two people too close to call apart."""
    scores: dict[str, float] = {}
    for person_id, stored in gallery:
        score = float(stored @ vector)
        scores[person_id] = max(score, scores.get(person_id, -1.0))
    if not scores:
        return None
    ranked = sorted(scores.values(), reverse=True)
    best_id = max(scores, key=lambda key: scores[key])
    runner_up = ranked[1] if len(ranked) > 1 else -1.0
    return best_id if ranked[0] >= accept and ranked[0] - runner_up >= margin else None
