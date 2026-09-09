# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Who a track is: each gated face, outfit and height measurement becomes a
calibrated log-likelihood ratio added to a running score per candidate person
(plus a "new person" hypothesis), and the consistency rules of RFC 5.2-5.4
(docs/rfc/people-memory.md in innate-jetson) decide when a score becomes an
identity and when it may change. :attr:`Identity.confidence` is that
accumulated posterior; no raw cosine leaves this module. PURE: numpy for the
cosine, no ROS, no cv2, no I/O — every persistent write goes through the
injected :class:`RosterView`."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from brain_client.people import quality
from brain_client.people.track import cosine
from brain_client.people.types import (
    SETTLED_STATES,
    Evidence,
    FaceTemplate,
    Identity,
    IdentityState,
    OutfitTemplate,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np

    from brain_client.people.types import (
        BodyObservation,
        FaceObservation,
        HeightEstimate,
        Pose,
        RosterView,
    )

NEW_PERSON = "__new__"
"""The open-world hypothesis: this face belongs to nobody on the roster."""


# ------------------------------------------------------------- calibration


@dataclass(frozen=True)
class Calibration:
    """Platt map from a cosine similarity to a log-likelihood ratio:
    ``logit P(same | s) = a.s + b``, fitted per embedding model on the
    calibration set. :meth:`between` builds the starting point out of the
    accept/reject pair the RFC quotes for that model."""

    a: float
    b: float

    def llr(self, similarity: float) -> float:
        return self.a * similarity + self.b

    @classmethod
    def between(cls, reject: float, accept: float, span: float = 2.0) -> Calibration:
        """The line through ``llr(reject) = -span`` and ``llr(accept) = +span``."""
        if accept <= reject:
            raise ValueError("accept must be above reject")
        a = 2.0 * span / (accept - reject)
        return cls(a=a, b=-span - a * reject)


@dataclass(frozen=True)
class FaceThresholds:
    """Cosine decision points for one face embedding space. Starting points to
    be calibrated on real recordings, not promises (RFC 5.2)."""

    accept: float
    reject: float
    margin: float
    self_similar: float  # pairwise floor among the frames that enrol a new person
    calibration: Calibration


SFACE_THRESHOLDS = FaceThresholds(
    accept=0.42, reject=0.30, margin=0.05, self_similar=0.38, calibration=Calibration.between(0.30, 0.42)
)
# The tree's prototype ran InspireFace, whose cosines sit higher for the same
# error rates; kept so a prototype recording resolves with its own numbers.
INSPIREFACE_THRESHOLDS = FaceThresholds(
    accept=0.50, reject=0.38, margin=0.06, self_similar=0.46, calibration=Calibration.between(0.38, 0.50)
)


@dataclass(frozen=True)
class BodyThresholds:
    """Outfit decision points: an outfit agrees at ``accept``, only when it
    beats the next-best outfit by ``margin`` (two people in similar clothes
    agree with nobody), and only ``min_frames`` such frames let outfit evidence
    count at all. The calibration is the Platt map through reject and accept,
    at half the face span: an outfit is a weaker cue per frame even when it
    matches perfectly."""

    accept: float = 0.60
    reject: float = 0.50
    margin: float = 0.07
    min_frames: int = 2
    calibration: Calibration = Calibration.between(0.50, 0.60, span=1.0)


OSNET_BODY_THRESHOLDS = BodyThresholds(
    accept=0.78, reject=0.68, margin=0.07, calibration=Calibration.between(0.68, 0.78, span=1.0)
)


def face_thresholds_for(model: str) -> FaceThresholds:
    """Thresholds for an embedding-space id such as ``"sface-2021-128"``."""
    return INSPIREFACE_THRESHOLDS if model.startswith("inspireface") else SFACE_THRESHOLDS


@dataclass(frozen=True)
class ResolverConfig:
    accept_log_odds: float = 4.0
    possible_log_odds: float = 1.5
    margin_log_odds: float = 1.5
    min_face_frames: int = 3
    min_face_span_sec: float = 1.0
    switch_margin_sec: float = 2.0
    # Everything that is not a face lives under this cap, which sits below
    # accept_log_odds: outfit, height and continuity together reach `possible`
    # and can never reach `known` (RFC 5.3.6).
    body_cap_log_odds: float = 3.0
    outfit_full_sec: float = 4 * 3600.0
    outfit_half_sec: float = 24 * 3600.0
    outfit_zero_sec: float = 48 * 3600.0
    height_weight: float = 0.4
    height_sigma_m: float = 0.06
    population_mean_m: float = 1.70
    population_sigma_m: float = 0.09
    enrol_face_frames: int = 5
    enrol_span_sec: float = 2.0
    min_track_sec: float = 2.0
    reconfirm_body_sec: float = 3.0
    template_interval_sec: float = 5.0
    height_interval_sec: float = 1.0
    sighting_interval_sec: float = 60.0
    max_frame_llr: float = 3.0
    max_total_log_odds: float = 12.0


DEFAULT_CONFIG = ResolverConfig()


def outfit_age_weight(age_sec: float, config: ResolverConfig = DEFAULT_CONFIG) -> float:
    """Full weight under 4 h, half at 24 h, nothing at 48 h (RFC 5.2). Clothes
    change; an outfit a face vouched for this morning is not evidence tomorrow."""
    if age_sec <= config.outfit_full_sec:
        return 1.0
    if age_sec >= config.outfit_zero_sec:
        return 0.0
    if age_sec <= config.outfit_half_sec:
        return 1.0 - 0.5 * (age_sec - config.outfit_full_sec) / (config.outfit_half_sec - config.outfit_full_sec)
    return 0.5 * (1.0 - (age_sec - config.outfit_half_sec) / (config.outfit_zero_sec - config.outfit_half_sec))


class TrackView(Protocol):
    """The little the resolver needs of a track; ``track.Track`` satisfies it."""

    @property
    def tag(self) -> str: ...
    @property
    def first_seen(self) -> float: ...
    @property
    def last_seen(self) -> float: ...
    @property
    def lost(self) -> bool: ...


@dataclass(frozen=True)
class Resolution:
    """One track's identity this tick, plus what the engine must act on."""

    tag: str
    identity: Identity
    split_requested: bool = False
    enrolled_id: str | None = None
    conflict_with: str | None = None  # the live tag already holding this person
    switched_from: str | None = None


# --------------------------------------------------------------- internals


@dataclass
class _Candidate:
    person_id: str
    face: float = 0.0
    body: float = 0.0
    height: float = 0.0
    continuity: float = 0.0
    face_frames: int = 0
    first_face: float | None = None
    last_face: float | None = None
    body_frames: int = 0
    body_agree_since: float | None = None
    last_accept_stamp: float | None = None

    def total(self, config: ResolverConfig, min_body_frames: int) -> float:
        # Outfit evidence only counts in this person's favour once it has agreed
        # on min_body_frames frames; a single agreeing frame is a coincidence.
        body = self.body if self.body <= 0.0 or self.body_frames >= min_body_frames else 0.0
        soft = min(body + self.height + self.continuity, config.body_cap_log_odds)
        return max(-config.max_total_log_odds, min(config.max_total_log_odds, self.face + soft))

    def face_span(self) -> float:
        if self.first_face is None or self.last_face is None:
            return 0.0
        return self.last_face - self.first_face


@dataclass(frozen=True)
class _PendingFace:
    """A frame kept for learning, with what it said about every person: it may
    only ever be written into the gallery it confirms."""

    observation: FaceObservation
    similarities: dict[str, float]


@dataclass
class _Belief:
    tag: str
    candidates: dict[str, _Candidate] = field(default_factory=dict)
    committed: str | None = None
    state: IdentityState = IdentityState.UNKNOWN
    committed_at: float = 0.0
    pressure_since: dict[str, float] = field(default_factory=dict)
    enrol_faces: list[FaceObservation] = field(default_factory=list)
    pending_faces: list[_PendingFace] = field(default_factory=list)
    best_roster_cosine: float = 0.0
    last_body: BodyObservation | None = None
    height_sum: float = 0.0
    height_count: int = 0
    height_variance: float = 0.0
    last_template_write: float = 0.0
    last_height_write: float = 0.0
    last_sighting_write: float = 0.0
    resumed: bool = False
    resumed_at: float = 0.0
    split_pending: bool = False
    face_model: str = ""

    def candidate(self, person_id: str) -> _Candidate:
        found = self.candidates.get(person_id)
        if found is None:
            found = _Candidate(person_id=person_id)
            self.candidates[person_id] = found
        return found

    @property
    def height_mean(self) -> float | None:
        return self.height_sum / self.height_count if self.height_count else None


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class Resolver:
    """Per-track evidence accumulation and the identity state machine.

    The engine feeds it gated observations, then calls :meth:`resolve` once per
    tick with every track it holds; the roster writes happen inside that call,
    so what was decided and what was learned can never disagree.
    """

    def __init__(
        self,
        roster: RosterView,
        *,
        face: FaceThresholds | None = None,
        body: BodyThresholds = BodyThresholds(),
        config: ResolverConfig = DEFAULT_CONFIG,
    ) -> None:
        self._roster = roster
        self._face_override = face
        self._body = body
        self._config = config
        self._beliefs: dict[str, _Belief] = {}
        self._suppressed: set[str] = set()

    @property
    def config(self) -> ResolverConfig:
        return self._config

    # -------------------------------------------------------------- evidence

    def observe_face(self, tag: str, observation: FaceObservation) -> None:
        """Fold one gated face frame into every candidate's score."""
        embedding = observation.embedding
        if embedding is None or not observation.model:
            return  # detection-only: it keeps the track alive, it says nothing about who
        belief = self._belief(tag)
        belief.face_model = observation.model
        thresholds = self._thresholds(observation.model)
        weight = observation.quality * _size_weight(observation.size_px)

        similarities = self._roster_similarities(embedding, observation.model)
        best_id, best_similarity = _best(similarities)
        belief.best_roster_cosine = max(belief.best_roster_cosine, best_similarity)

        for person_id, similarity in similarities.items():
            candidate = belief.candidate(person_id)
            raw = thresholds.calibration.llr(similarity)
            candidate.face += _clamp(raw * weight, self._config.max_frame_llr)
            if raw > 0.0:
                candidate.face_frames += 1
                if candidate.first_face is None:
                    candidate.first_face = observation.stamp
                candidate.last_face = observation.stamp
            if similarity >= thresholds.accept:
                candidate.last_accept_stamp = observation.stamp

        # One-vs-rest: a frame that matches nobody is evidence for a new person.
        against_new = thresholds.calibration.llr(best_similarity if similarities else thresholds.reject)
        belief.candidate(NEW_PERSON).face += _clamp(-against_new * weight, self._config.max_frame_llr)

        self._note_split_pressure(belief, thresholds, similarities, best_id, best_similarity)
        self._collect_for_learning(belief, observation, thresholds, similarities, best_similarity)

    def observe_body(self, tag: str, observation: BodyObservation, now: float) -> None:
        """Fold one gated outfit frame in. Body evidence never names anybody: it
        feeds candidates that already exist, and lives under the body cap."""
        belief = self._belief(tag)
        belief.last_body = observation
        embedding = observation.embedding
        if embedding is None or not observation.model:
            return
        weight = quality.body_quality_score(height_px=observation.height_px, score=1.0, sharpness=observation.sharpness)
        scored: dict[str, tuple[float, float]] = {}
        for person_id in self._roster.person_ids():
            best = self._best_outfit(person_id, embedding, observation.model, now)
            if best is not None:  # None: nothing on file, or every outfit is past 48 h
                scored[person_id] = best
        agreeing = self._agreeing_outfit(scored)
        for person_id, (llr, _similarity) in scored.items():
            candidate = belief.candidate(person_id)
            candidate.body += _clamp(llr * weight, self._config.max_frame_llr)
            if person_id != agreeing:
                candidate.body_agree_since = None
                continue
            candidate.body_frames += 1
            if candidate.body_agree_since is None:
                candidate.body_agree_since = observation.stamp

    def _agreeing_outfit(self, scored: dict[str, tuple[float, float]]) -> str | None:
        """The one person this outfit frame agrees with, or None. Two people in
        similar clothes agree with nobody: the margin is what says so."""
        ranked = sorted(scored.items(), key=lambda item: -item[1][1])
        if not ranked or ranked[0][1][1] < self._body.accept:
            return None
        runner_up = ranked[1][1][1] if len(ranked) > 1 else -1.0
        return ranked[0][0] if ranked[0][1][1] - runner_up >= self._body.margin else None

    def _best_outfit(self, person_id: str, embedding: np.ndarray, model: str, now: float) -> tuple[float, float] | None:
        """The strongest live outfit as (age-weighted llr, its raw cosine). An
        outfit's evidence fades with its age, so an expired one is silence
        rather than a mismatch."""
        best: tuple[float, float] | None = None
        for outfit in self._roster.outfits(person_id, model, now):
            if outfit.model != model:
                continue  # embeddings of different spaces are not comparable
            age_weight = outfit_age_weight(now - outfit.stamp, self._config)
            if age_weight <= 0.0:
                continue
            similarity = cosine(embedding, outfit.embedding)
            scored = (self._body.calibration.llr(similarity) * age_weight, similarity)
            if best is None or scored[0] > best[0]:
                best = scored
        return best

    def observe_height(self, tag: str, height_m: float, variance: float) -> None:
        """A weak Gaussian tie-breaker. Height is a state estimate, not a stream
        of independent frames, so it is recomputed rather than accumulated."""
        belief = self._belief(tag)
        belief.height_sum += height_m
        belief.height_count += 1
        belief.height_variance = variance
        mean = belief.height_mean
        if mean is None:
            return
        for person_id in self._roster.person_ids():
            stored = self._roster.height(person_id)
            if stored is None:
                continue
            belief.candidate(person_id).height = self._height_llr(mean, stored, variance)

    # ------------------------------------------------------------- lifecycle

    def identity(self, tag: str) -> Identity:
        belief = self._beliefs.get(tag)
        return self._identity_of(belief) if belief is not None else Identity()

    def forget(self, tag: str) -> None:
        """Drop everything believed about a tag and stop it enrolling.

        RFC section 8: forgetting a person suppresses their live track. The
        person is standing in front of the robot when they ask, and without
        this the next five face frames put them straight back on the roster
        under a new id. The suppression dies with the tag (tags are never
        reused), so the next track to carry one enrols normally.
        """
        self._beliefs.pop(tag, None)
        self._suppressed.add(tag)

    def rebind(self, old_id: str, new_id: str) -> None:
        """RFC section 8: ``merge_people`` tombstones ``old_id``, so every live
        belief in it moves across rather than being dropped — it was one person
        all along, and the track in front of the robot is still theirs."""
        for belief in self._beliefs.values():
            stale = belief.candidates.pop(old_id, None)
            belief.pressure_since.pop(old_id, None)
            if stale is not None:
                _absorb(belief.candidate(new_id), stale)
            if belief.committed == old_id:
                belief.committed = new_id
                belief.state = self._committed_state(new_id)

    def apply_split(self, tag: str, new_tag: str) -> None:
        """RFC 5.3.4: both tags resolve afresh, and everything learned since the
        last confirmation is dropped rather than written onto the wrong person."""
        self._beliefs.pop(tag, None)
        self._beliefs.pop(new_tag, None)

    def on_reassociated(self, tag: str, now: float) -> None:
        """A lost track that walked back in resumes at ``possible`` and needs a
        face frame, or 3 s of agreeing outfit, before it is a name again."""
        belief = self._beliefs.get(tag)
        if belief is None or belief.committed is None:
            return
        belief.resumed = True
        belief.resumed_at = now
        belief.state = IdentityState.POSSIBLE
        belief.pending_faces.clear()
        # The run of agreeing outfit frames restarts with the track: reconfirmation
        # wants agreement since the resume, and a stamp taken before the gap is
        # never replaced while the frames keep agreeing.
        for candidate in belief.candidates.values():
            candidate.body_agree_since = None
            candidate.body_frames = 0

    # --------------------------------------------------------------- resolve

    def resolve(
        self,
        tracks: Sequence[TrackView],
        now: float,
        *,
        map_name: str | None = None,
        pose: Pose | None = None,
    ) -> dict[str, Resolution]:
        live = [t for t in tracks if not t.lost]
        resolutions = {track.tag: self._resolve_one(track, now) for track in live}
        self._enforce_one_live_track(live, resolutions)
        for track in live:
            self._learn(track.tag, resolutions[track.tag], now, map_name, pose)
        known_tags = {t.tag for t in tracks}
        for tag in [t for t in self._beliefs if t not in known_tags]:
            del self._beliefs[tag]
        self._suppressed &= known_tags
        return resolutions

    def _resolve_one(self, track: TrackView, now: float) -> Resolution:
        belief = self._belief(track.tag)
        if belief.committed is not None:
            belief.candidate(belief.committed).continuity = self._config.possible_log_odds

        if belief.split_pending:
            belief.split_pending = False
            belief.pending_faces.clear()  # they were collected while the track meant someone else
            return Resolution(tag=track.tag, identity=self._identity_of(belief), split_requested=True)

        if belief.committed is None:
            return self._resolve_uncommitted(belief, track, now)
        return self._resolve_committed(belief, now)

    def _resolve_uncommitted(self, belief: _Belief, track: TrackView, now: float) -> Resolution:
        ranked = self._ranked(belief)
        best_id, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else -self._config.max_total_log_odds

        if best_id != NEW_PERSON and self._qualifies(belief, best_id, best_score, runner_up):
            self._commit(belief, best_id, now)
            return Resolution(tag=belief.tag, identity=self._identity_of(belief))

        enrolled = self._try_enrol(belief, track, now) if best_id == NEW_PERSON else None
        if enrolled is not None:
            self._commit(belief, enrolled, now)
            return Resolution(tag=belief.tag, identity=self._identity_of(belief), enrolled_id=enrolled)

        belief.state = self._tentative_state(best_id, best_score, runner_up)
        return Resolution(tag=belief.tag, identity=self._identity_of(belief))

    def _resolve_committed(self, belief: _Belief, now: float) -> Resolution:
        switched_from = self._maybe_switch(belief, now)
        self._maybe_reconfirm(belief, now)
        committed = belief.committed
        if committed is not None and not belief.resumed:
            mine, other = self._score_of(self._ranked(belief), committed)
            if self._qualifies(belief, committed, mine, other):
                belief.state = self._committed_state(committed)
        return Resolution(tag=belief.tag, identity=self._identity_of(belief), switched_from=switched_from)

    # ------------------------------------------------------------- decisions

    def _ranked(self, belief: _Belief) -> list[tuple[str, float]]:
        scores = [(c.person_id, c.total(self._config, self._body.min_frames)) for c in belief.candidates.values()]
        scores.sort(key=lambda item: (-item[1], item[0]))
        return scores

    @staticmethod
    def _score_of(ranked: Sequence[tuple[str, float]], person_id: str) -> tuple[float, float]:
        """(this person's score, the best score that is not theirs)."""
        mine = next((score for pid, score in ranked if pid == person_id), 0.0)
        other = next((score for pid, score in ranked if pid != person_id), -1e9)
        return mine, other

    def _qualifies(self, belief: _Belief, person_id: str, score: float, runner_up: float) -> bool:
        """The accept line: accumulated evidence, a margin over the runner-up,
        and three independent face frames spanning a second."""
        candidate = belief.candidates.get(person_id)
        if person_id == NEW_PERSON or candidate is None:
            return False
        return (
            score >= self._config.accept_log_odds
            and score - runner_up >= self._config.margin_log_odds
            and candidate.face_frames >= self._config.min_face_frames
            and candidate.face_span() >= self._config.min_face_span_sec
        )

    def _tentative_state(self, best_id: str, best_score: float, runner_up: float) -> IdentityState:
        if best_id == NEW_PERSON or best_score < self._config.possible_log_odds or best_score <= runner_up:
            return IdentityState.UNKNOWN
        return IdentityState.POSSIBLE

    def _commit(self, belief: _Belief, person_id: str, now: float) -> None:
        belief.committed = person_id
        belief.committed_at = now
        belief.resumed = False
        belief.candidate(person_id).continuity = self._config.possible_log_odds
        belief.state = self._committed_state(person_id)

    def _committed_state(self, person_id: str) -> IdentityState:
        return IdentityState.KNOWN if self._roster.name_of(person_id) else IdentityState.FAMILIAR

    def _maybe_switch(self, belief: _Belief, now: float) -> str | None:
        """Hysteresis (RFC 5.3.3): B must lead A by the margin for two seconds
        of evidence. Until then the track shows A and the pressure is logged.
        The open-world hypothesis is a rival like any other, but it is nobody to
        commit to: when it wins, the tracker walked the box onto a stranger and
        the track splits instead (RFC 5.3.4)."""
        current = belief.committed
        if current is None:
            return None
        ranked = self._ranked(belief)
        mine, _ = self._score_of(ranked, current)
        for person_id, score in ranked:
            if person_id == current:
                continue
            if score - mine < self._config.margin_log_odds:
                belief.pressure_since.pop(person_id, None)
                continue
            since = belief.pressure_since.setdefault(person_id, now)
            if now - since < self._config.switch_margin_sec:
                continue
            belief.pressure_since.clear()
            if person_id == NEW_PERSON:
                belief.split_pending = True
                return None
            belief.pending_faces.clear()  # they were collected while the track meant someone else
            belief.candidate(current).continuity = 0.0
            self._commit(belief, person_id, now)
            if not self._qualifies(belief, person_id, score, mine):
                belief.state = IdentityState.POSSIBLE
            return current
        return None

    def _maybe_reconfirm(self, belief: _Belief, now: float) -> None:
        """A resumed track returns to its committed state on one accepting face
        frame or three seconds of agreeing outfit."""
        committed = belief.committed
        if not belief.resumed or committed is None:
            return
        candidate = belief.candidates.get(committed)
        if candidate is None:
            return
        accepted = candidate.last_accept_stamp
        face_confirmed = accepted is not None and accepted > belief.resumed_at
        agreeing_since = candidate.body_agree_since
        body_confirmed = (
            agreeing_since is not None
            and agreeing_since >= belief.resumed_at
            and now - agreeing_since >= self._config.reconfirm_body_sec
        )
        if face_confirmed or body_confirmed:
            belief.resumed = False
            belief.state = self._committed_state(committed)

    def _note_split_pressure(
        self,
        belief: _Belief,
        thresholds: FaceThresholds,
        similarities: dict[str, float],
        best_id: str | None,
        best_similarity: float,
    ) -> None:
        """RFC 5.3.4: a face frame that accepts B on a track committed to A means
        the tracker swapped two people. Renaming the track would carry A's
        history onto B, so the track is split instead."""
        committed = belief.committed
        if committed is None or belief.state not in SETTLED_STATES:
            return
        if best_id is None or best_id == committed or best_similarity < thresholds.accept:
            return
        if best_similarity - similarities.get(committed, 0.0) < thresholds.margin:
            return
        belief.split_pending = True

    def _enforce_one_live_track(self, live: Sequence[TrackView], resolutions: dict[str, Resolution]) -> None:
        """RFC 5.3.5: two concurrent tracks are never the same person; the later
        claim is held at ``possible`` and flagged for the log and the owner."""
        holder: dict[str, str] = {}
        for track in sorted(live, key=lambda t: (t.first_seen, t.tag)):
            resolution = resolutions[track.tag]
            person_id = resolution.identity.person_id
            if person_id is None or resolution.identity.state not in SETTLED_STATES:
                continue
            first = holder.get(person_id)
            if first is None:
                holder[person_id] = track.tag
                continue
            belief = self._belief(track.tag)
            belief.state = IdentityState.POSSIBLE
            resolutions[track.tag] = Resolution(
                tag=track.tag,
                identity=self._identity_of(belief),
                enrolled_id=resolution.enrolled_id,
                conflict_with=first,
                switched_from=resolution.switched_from,
            )

    # -------------------------------------------------------------- learning

    def _collect_for_learning(
        self,
        belief: _Belief,
        observation: FaceObservation,
        thresholds: FaceThresholds,
        similarities: dict[str, float],
        best_similarity: float,
    ) -> None:
        if not quality.face_size_ok(observation.size_px, quality.Purpose.ENROL, real_px=observation.real_px):
            return
        if not quality.face_pose_ok(observation.yaw_deg, observation.pitch_deg, quality.Purpose.ENROL):
            return
        belief.pending_faces.append(_PendingFace(observation=observation, similarities=dict(similarities)))
        del belief.pending_faces[:-10]
        if best_similarity <= thresholds.reject:
            belief.enrol_faces.append(observation)
            del belief.enrol_faces[:-10]

    def _try_enrol(self, belief: _Belief, track: TrackView, now: float) -> str | None:
        """RFC 5.4: five self-consistent face frames over two seconds matching
        nobody, on a track that has been here long enough to be a person rather
        than a passer-by. Body evidence never gets here."""
        config = self._config
        if track.tag in self._suppressed or now - track.first_seen < config.min_track_sec:
            return None
        if not self._roster.collection_enabled() or not self._roster.can_enrol():
            return None
        # The whole buffer, not its last five: frames arriving faster than one
        # per 0.4 s would otherwise never span the two seconds, and a person who
        # keeps looking at the robot would never enrol.
        faces = list(belief.enrol_faces)
        if len(faces) < config.enrol_face_frames or faces[-1].stamp - faces[0].stamp < config.enrol_span_sec:
            return None
        thresholds = self._thresholds(belief.face_model)
        if belief.best_roster_cosine > thresholds.reject or not _self_consistent(faces, thresholds.self_similar):
            return None
        templates = [_template(face, face.embedding) for face in faces if face.embedding is not None]
        if len(templates) < config.enrol_face_frames:
            return None
        thumbnail = next((face.thumbnail for face in reversed(faces) if face.thumbnail), None)
        person_id = self._roster.create_unnamed(templates, thumbnail, now)
        if not person_id:
            return None  # the roster filled up between can_enrol and the write; try again next tick
        belief.enrol_faces.clear()
        belief.pending_faces.clear()
        belief.last_template_write = now
        candidate = belief.candidate(person_id)
        candidate.face = config.accept_log_odds + config.margin_log_odds
        candidate.face_frames = len(faces)
        candidate.first_face = faces[0].stamp
        candidate.last_face = faces[-1].stamp
        candidate.last_accept_stamp = faces[-1].stamp
        belief.candidate(NEW_PERSON).face = 0.0
        return person_id

    def _learn(
        self,
        tag: str,
        resolution: Resolution,
        now: float,
        map_name: str | None,
        pose: Pose | None,
    ) -> None:
        belief = self._belief(tag)
        person_id = belief.committed
        if person_id is None or belief.state not in SETTLED_STATES:
            return
        if resolution.split_requested or resolution.conflict_with is not None:
            return  # the evidence disagrees: nothing this track saw is safe to write
        if not self._roster.collection_enabled():
            return
        if now - belief.last_sighting_write >= self._config.sighting_interval_sec:
            belief.last_sighting_write = now
            self._roster.record_sighting(person_id, now, map_name, pose)
        self._write_face(belief, person_id, now)
        self._write_height(belief, person_id, now)

    def _write_face(self, belief: _Belief, person_id: str, now: float) -> None:
        if not belief.pending_faces or now - belief.last_template_write < self._config.template_interval_sec:
            return
        # Only a frame that accepts this person is theirs: a track the tracker
        # walked onto a stranger stays committed until the split, and the
        # stranger's face must not end up in the gallery it was standing in.
        accept = self._thresholds(belief.face_model).accept
        confirming = [pending for pending in belief.pending_faces if pending.similarities.get(person_id, 0.0) >= accept]
        belief.pending_faces.clear()
        best = max(confirming, key=lambda pending: pending.observation.quality, default=None)
        if best is None:
            return
        face = best.observation
        if face.embedding is None:
            return
        belief.last_template_write = now
        self._roster.add_face_template(person_id, _template(face, face.embedding), face.thumbnail)
        body = belief.last_body
        if body is not None and body.embedding is not None and body.model:
            # Today's outfit is snapshotted on a face confirmation and only then:
            # from behind, the engine matches what the face vouched for.
            self._roster.add_outfit(person_id, OutfitTemplate(embedding=body.embedding, model=body.model, stamp=now))

    def _write_height(self, belief: _Belief, person_id: str, now: float) -> None:
        mean = belief.height_mean
        if mean is None or now - belief.last_height_write < self._config.height_interval_sec:
            return
        belief.last_height_write = now
        self._roster.add_height_sample(person_id, mean, belief.height_variance)

    # ---------------------------------------------------------------- pieces

    def _belief(self, tag: str) -> _Belief:
        belief = self._beliefs.get(tag)
        if belief is None:
            belief = _Belief(tag=tag)
            belief.candidate(NEW_PERSON)  # the open-world denominator of every posterior
            self._beliefs[tag] = belief
        return belief

    def _thresholds(self, model: str) -> FaceThresholds:
        return self._face_override or face_thresholds_for(model)

    def _roster_similarities(self, embedding: np.ndarray, model: str) -> dict[str, float]:
        similarities: dict[str, float] = {}
        for person_id in self._roster.person_ids():
            templates = [t for t in self._roster.face_templates(person_id, model) if t.model == model]
            if not templates:
                continue  # a template from another embedding space is not comparable
            similarities[person_id] = max(cosine(embedding, t.embedding) for t in templates)
        return similarities

    def _height_llr(self, measured_m: float, stored: HeightEstimate, variance: float) -> float:
        sigma = math.sqrt(max(stored.variance, 0.0) + max(variance, 0.0) + self._config.height_sigma_m**2)
        person = -0.5 * ((measured_m - stored.mean_m) / sigma) ** 2 - math.log(sigma)
        population = -0.5 * (
            (measured_m - self._config.population_mean_m) / self._config.population_sigma_m
        ) ** 2 - math.log(self._config.population_sigma_m)
        return self._config.height_weight * _clamp(person - population, 2.0)

    def _identity_of(self, belief: _Belief) -> Identity:
        ranked = self._ranked(belief)
        person_id = belief.committed
        if person_id is None and belief.state is IdentityState.POSSIBLE:
            person_id = next((pid for pid, _ in ranked if pid != NEW_PERSON), None)
        if person_id is None:
            leader = next((pid for pid, _ in ranked if pid != NEW_PERSON), None)
            return Identity(
                state=IdentityState.UNKNOWN,
                runner_up_id=leader,
                runner_up_confidence=self._posterior(ranked, leader),
                runner_up_name=self._roster.name_of(leader) if leader else None,
            )
        runner_up = next((pid for pid, _ in ranked if pid not in (person_id, NEW_PERSON)), None)
        return Identity(
            state=belief.state,
            person_id=person_id,
            name=self._roster.name_of(person_id),
            confidence=self._posterior(ranked, person_id),
            evidence=self._evidence(belief, person_id),
            runner_up_id=runner_up,
            runner_up_confidence=self._posterior(ranked, runner_up),
            runner_up_name=self._roster.name_of(runner_up) if runner_up else None,
        )

    @staticmethod
    def _posterior(ranked: Sequence[tuple[str, float]], person_id: str | None) -> float:
        """The accumulated posterior over the candidates and the new-person
        hypothesis — what :attr:`Identity.confidence` carries, never a cosine."""
        if person_id is None or not ranked:
            return 0.0
        top = ranked[0][1]
        weights = {pid: math.exp(max(-50.0, score - top)) for pid, score in ranked}
        total = sum(weights.values())
        return weights.get(person_id, 0.0) / total if total > 0.0 else 0.0

    @staticmethod
    def _evidence(belief: _Belief, person_id: str) -> tuple[Evidence, ...]:
        candidate = belief.candidates.get(person_id)
        if candidate is None:
            return ()
        found: list[Evidence] = []
        if candidate.face_frames:
            found.append(Evidence.FACE)
        if candidate.body_frames:
            found.append(Evidence.OUTFIT)
        if candidate.continuity:
            found.append(Evidence.CONTINUITY)
        if candidate.height:
            found.append(Evidence.HEIGHT)
        return tuple(found)


def _absorb(candidate: _Candidate, stale: _Candidate) -> None:
    """Fold one merged person's accumulation into the other's. The stronger of
    the two, never the sum: every frame scored both candidates, so adding them
    would count the same evidence twice."""
    candidate.face = max(candidate.face, stale.face)
    candidate.body = max(candidate.body, stale.body)
    candidate.height = max(candidate.height, stale.height)
    candidate.continuity = max(candidate.continuity, stale.continuity)
    candidate.face_frames = max(candidate.face_frames, stale.face_frames)
    candidate.body_frames = max(candidate.body_frames, stale.body_frames)
    candidate.first_face = _first(candidate.first_face, stale.first_face)
    candidate.last_face = _last(candidate.last_face, stale.last_face)
    candidate.body_agree_since = _first(candidate.body_agree_since, stale.body_agree_since)
    candidate.last_accept_stamp = _last(candidate.last_accept_stamp, stale.last_accept_stamp)


def _first(*stamps: float | None) -> float | None:
    known = [stamp for stamp in stamps if stamp is not None]
    return min(known) if known else None


def _last(*stamps: float | None) -> float | None:
    known = [stamp for stamp in stamps if stamp is not None]
    return max(known) if known else None


def _best(similarities: dict[str, float]) -> tuple[str | None, float]:
    if not similarities:
        return (None, 0.0)
    person_id = max(similarities, key=lambda pid: similarities[pid])
    return (person_id, similarities[person_id])


def _size_weight(size_px: float) -> float:
    """A 48 px face carries full weight, a 24 px one half (RFC 5.2: face
    evidence is scaled by quality and by size)."""
    return max(0.5, min(1.5, size_px / quality.FACE_MIN_ENROL_PX))


def _self_consistent(faces: Sequence[FaceObservation], floor: float) -> bool:
    for index, first in enumerate(faces):
        for second in faces[index + 1 :]:
            if first.embedding is None or second.embedding is None:
                return False
            if cosine(first.embedding, second.embedding) < floor:
                return False
    return True


def _template(observation: FaceObservation, embedding: np.ndarray) -> FaceTemplate:
    return FaceTemplate(
        embedding=embedding,
        model=observation.model,
        stamp=observation.stamp,
        pose_bucket=quality.pose_bucket(observation.yaw_deg, observation.pitch_deg),
        quality=observation.quality,
    )
