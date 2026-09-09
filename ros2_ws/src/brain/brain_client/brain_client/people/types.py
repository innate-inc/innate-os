# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared vocabulary of the people engine: the values every stage passes along
and the protocols the stages are written against (see docs/rfc/people-memory.md
in innate-jetson).

PURE module: no rclpy, no cv2. Boxes are normalized ``(ymin, xmin, ymax, xmax)``
in [0, 1] of the published 640x480 left frame — the frame every consumer (the
brain's overlay, the webapp, skills) reasons in; native-resolution pixels only
ever exist inside a crop. Stamps are epoch seconds (``time.time()``), never
monotonic: they are compared with ROS header stamps and persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypedDict

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    import numpy as np

Box = tuple[float, float, float, float]
"""Normalized (ymin, xmin, ymax, xmax) in the published frame."""

Pose = tuple[float, float, float]
"""(x, y, theta) in the map frame."""

SNAPSHOT_SCHEMA = 1


class IdentityState(StrEnum):
    """A track's display state; wire-visible, never change the values."""

    UNKNOWN = "unknown"
    FAMILIAR = "familiar"
    POSSIBLE = "possible"
    KNOWN = "known"
    CONFLICT = "conflict"


SETTLED_STATES = (IdentityState.KNOWN, IdentityState.FAMILIAR)
"""Face-confirmed and committed: the resolver learns onto these, the engine
stops spending face crops on them, and attention moves on to somebody else."""


class Evidence(StrEnum):
    FACE = "face"
    OUTFIT = "outfit"
    CONTINUITY = "continuity"
    HEIGHT = "height"
    LEGS = "legs"


class HealthState(StrEnum):
    OK = "ok"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    NONE = "none"


class EventKind(StrEnum):
    """``/brain/people_events`` kinds; wire-visible."""

    ENROLLED = "enrolled"
    REENTERED = "reentered"
    NAME_LEARNED = "name_learned"
    DISAMBIGUATION = "disambiguation"
    CONFLICT = "conflict"
    RECALLED = "recalled"


# ---------------------------------------------------------------- perception


@dataclass(frozen=True)
class Detection:
    """One person (or lone face) found in a frame."""

    box: Box
    score: float
    head_box: Box | None = None
    source: str = "body"  # "body" from the person detector, "face" from a lone face


@dataclass(frozen=True)
class FaceHit:
    """A face located inside a crop: pixel box and five landmarks
    (left eye, right eye, nose, left mouth, right mouth) in crop pixels."""

    x: float
    y: float
    w: float
    h: float
    landmarks: tuple[tuple[float, float], ...]
    score: float


@dataclass(frozen=True)
class FaceObservation:
    """A gated face measurement on one frame. ``embedding`` is None when the
    quality gates rejected the crop for matching but the face still counts
    for detection (keeping the track alive, feeding height)."""

    stamp: float
    box: Box
    size_px: float  # face height in NATIVE pixels
    real_px: float  # face height in the crop's own pixels; equal to size_px on the native path
    yaw_deg: float
    pitch_deg: float  # positive = seen from below
    sharpness: float
    luminance: float
    quality: float  # 0-1, the evidence weight after gates
    model: str  # embedding space id, e.g. "sface-2021-128"; "" when no embedding
    embedding: np.ndarray | None = None  # L2-normalized
    thumbnail: bytes | None = None  # JPEG crop, for enrolment thumbnails


@dataclass(frozen=True)
class BodyObservation:
    stamp: float
    box: Box
    height_px: float  # box height in NATIVE pixels
    sharpness: float
    model: str
    embedding: np.ndarray | None = None


# ------------------------------------------------------------------ identity


@dataclass(frozen=True)
class Identity:
    state: IdentityState = IdentityState.UNKNOWN
    person_id: str | None = None
    name: str | None = None
    confidence: float = 0.0  # accumulated, 0-1; never a raw cosine
    evidence: tuple[Evidence, ...] = ()
    runner_up_id: str | None = None
    runner_up_confidence: float = 0.0
    runner_up_name: str | None = None  # for the conflict wording "unsure (Theo or Ana)"


@dataclass(frozen=True)
class TrackState:
    """What the engine hands surfacing every tick: one per live track and per
    recently lost track (``lost`` True)."""

    tag: str  # "P3"; stable for the track's life, never reused in a session
    box: Box
    head_box: Box | None
    identity: Identity
    first_seen: float
    last_seen: float
    lost: bool = False
    range_m: float | None = None
    bearing_deg: float | None = None  # positive = left of the robot
    height_m: float | None = None
    speaking: bool = False  # speech arrived while this track was the plausible speaker
    frames_with_face: int = 0
    last_face_stamp: float | None = None


# --------------------------------------------------------------------- roster


@dataclass(frozen=True)
class FaceTemplate:
    embedding: np.ndarray
    model: str
    stamp: float
    pose_bucket: str  # "frontal" | "left" | "right" | "up" | "down"
    quality: float
    thumbnail_id: str | None = None


@dataclass(frozen=True)
class OutfitTemplate:
    embedding: np.ndarray
    model: str
    stamp: float
    thumbnail_id: str | None = None


@dataclass(frozen=True)
class HeightEstimate:
    mean_m: float
    variance: float
    samples: int


class RosterView(Protocol):
    """What the resolver needs from the store. The store implements it; tests
    use a fake. Every method is safe to call from the engine thread."""

    def person_ids(self) -> list[str]: ...
    def name_of(self, person_id: str) -> str | None: ...
    def face_templates(self, person_id: str, model: str) -> list[FaceTemplate]: ...
    def outfits(self, person_id: str, model: str, now: float) -> list[OutfitTemplate]: ...
    def height(self, person_id: str) -> HeightEstimate | None: ...
    def collection_enabled(self) -> bool: ...
    def can_enrol(self) -> bool: ...
    def create_unnamed(self, faces: list[FaceTemplate], thumbnail: bytes | None, now: float) -> str: ...
    def add_face_template(self, person_id: str, template: FaceTemplate, thumbnail: bytes | None) -> None: ...
    def add_outfit(self, person_id: str, outfit: OutfitTemplate) -> None: ...
    def add_height_sample(self, person_id: str, height_m: float, variance: float) -> None: ...
    def record_sighting(self, person_id: str, now: float, map_name: str | None, pose: Pose | None) -> None: ...


# ------------------------------------------------------------------- backends


class PersonDetector(Protocol):
    name: str

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        """Boxes normalized to the frame given (the un-squashed published frame)."""
        ...


class FaceLocator(Protocol):
    name: str

    def locate(self, crop_bgr: np.ndarray) -> list[FaceHit]: ...


class FaceEmbedder(Protocol):
    model: str

    def embed(self, crop_bgr: np.ndarray, hit: FaceHit) -> np.ndarray:
        """Aligns on the landmarks and returns an L2-normalized vector."""
        ...


class BodyEmbedder(Protocol):
    model: str

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray: ...


# ----------------------------------------------------------------- wire format
# The JSON on /brain/people and /brain/people_events. Boxes here are Gemini's
# per-mille ints [ymin, xmin, ymax, xmax]; stamps are epoch seconds; the source
# frame's ROS header stamp rides as a decimal nanosecond STRING (JS consumers).


class FactDict(TypedDict, total=False):
    id: str
    text: str
    kind: str
    importance: float
    last_confirmed: float


class OpenLoopDict(TypedDict, total=False):
    id: str
    text: str
    due: str | None


class EpisodeDict(TypedDict, total=False):
    start: float
    end: float | None
    map: str | None
    summary: str


class LastSeenDict(TypedDict, total=False):
    stamp: float
    map: str | None
    x: float | None
    y: float | None


class PersonDigestDict(TypedDict, total=False):
    facts: list[FactDict]
    open_loops: list[OpenLoopDict]
    episodes: list[EpisodeDict]
    last_seen: LastSeenDict | None
    encounters: int


class PersonInViewDict(TypedDict, total=False):
    tag: str
    person_id: str | None
    name: str | None
    state: str
    evidence: list[str]
    confidence: float
    runner_up_name: str | None
    bbox: list[int]
    head_bbox: list[int] | None
    range_m: float | None
    bearing_deg: float | None
    tracked_sec: float
    lost: bool
    description: str | None
    hint: str | None  # e.g. "heard 'I'm Ana' but two people are in view; if it matters, ask which one"
    learned: str | None  # e.g. "P5 said \"I'm Zoe\" — P5 = Zoe from here on"; shown once
    digest: PersonDigestDict | None


class AttentionDict(TypedDict, total=False):
    tag: str
    text: str
    head_bbox: list[int] | None


class HealthDict(TypedDict, total=False):
    camera: str
    native: str
    face_model: str
    body_model: str
    gpu: str
    scribe: str  # none | unavailable | ok | stale


class RecentPersonDict(TypedDict, total=False):
    person_id: str
    name: str | None
    last_seen: float
    map: str | None


class PeopleSnapshotDict(TypedDict, total=False):
    schema: int
    stamp: float
    frame_stamp_ns: str | None
    image_size: list[int]
    health: HealthDict
    collection_enabled: bool
    attention: AttentionDict | None
    people: list[PersonInViewDict]
    recent: list[RecentPersonDict]


class RosterEntryDict(TypedDict, total=False):
    """One roster row for the Settings page (``GetPeople`` with ``include_roster``)."""

    person_id: str
    name: str | None
    unnamed: bool
    encounters: int
    last_seen: LastSeenDict | None
    description: str | None
    thumbnail: str | None  # base64 JPEG, only with ``include_thumbnails``


class PeopleRosterDict(PeopleSnapshotDict, total=False):
    """The ``GetPeople`` answer: the live snapshot plus the roster. A missing
    ``roster`` key reads as "service unavailable" in the webapp, never as empty."""

    roster: list[RosterEntryDict]
    capacity_full: bool


class PeopleEventDict(TypedDict, total=False):
    kind: str
    stamp: float
    tag: str | None
    person_id: str | None
    name: str | None
    text: str
    image_b64: str | None
