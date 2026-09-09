# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS glue for the people node: the sensors the engine reads, the two
publishers, the five services and the two worker threads; every decision is
the engine's, the store's, the scribe's or surfacing's (RFC sections 3-6,
docs/rfc/people-memory.md in innate-jetson). Callbacks stash a message under
one lock and return; the engine ticks on its own thread and is the only
publisher; the scribe works on a second thread and hands results back through
queues. Subscriptions are created and destroyed only on the executor thread
(destroying one under a spinning executor is an InvalidHandle race), so worker
threads raise flags for a 1 Hz timer. The tick is sampled from the 7.5 Hz
compressed left topic — not chased, and not the 15 Hz raw one — because the
decoded frame's header stamp is the ``frame_stamp_ns`` the brain pairs its
overlay by. Everything above the "ROS adapters" banner is pure and tested in
test/test_people_node.py."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from brain_messages.srv import ForgetPerson, GetPeople, MergePeople, RenamePerson, SetPeopleCollection
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from std_msgs.msg import String

from brain_client.common.enums import StrEnum
from brain_client.common.geometry import quaternion_to_yaw
from brain_client.common.script_paths import get_innate_os_root
from brain_client.people import description, native_frames
from brain_client.people.geometry import CameraModel
from brain_client.people.quality import EgoMotionTracker
from brain_client.people.scribe import ChangeKind, Speaker, TagView, Utterance, is_memory_question
from brain_client.people.surfacing import PeopleEvents, build_snapshot, choose_attention
from brain_client.people.types import SNAPSHOT_SCHEMA, HealthDict, HealthState, IdentityState, TrackState
from brain_client.perception.motion_gate import MotionGate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from rclpy.node import Node
    from rclpy.publisher import Publisher
    from rclpy.subscription import Subscription

    from brain_client.people.engine import EngineConfig, PeopleEngine
    from brain_client.people.quality import EgoMotion
    from brain_client.people.resolve import Resolution
    from brain_client.people.scribe import Change, Scribe, Transport
    from brain_client.people.store import PeopleStore
    from brain_client.people.types import (
        AttentionDict,
        PeopleRosterDict,
        PeopleSnapshotDict,
        Pose,
        RosterEntryDict,
    )

SNAPSHOT_TOPIC = "/brain/people"
EVENTS_TOPIC = "/brain/people_events"
COMPRESSED_IMAGE_TOPIC = "/mars/main_camera/left/image_raw/compressed"
RAW_IMAGE_TOPIC = "/mars/main_camera/left/image_raw"
CAMERA_INFO_TOPIC = "/mars/main_camera/left/camera_info"
NATIVE_TOPIC = "/mars/main_camera/native/compressed"
HEAD_TOPIC = "/mars/head/current_position"
CMD_VEL_TOPIC = "/cmd_vel"
ODOM_TOPIC = "/odom"
AMCL_POSE_TOPIC = "/amcl_pose"
CURRENT_MAP_TOPIC = "/nav/current_map"
AGENT_STATUS_TOPIC = "/brain/agent_status"
CHAT_IN_TOPIC = "/brain/chat_in"
CHAT_OUT_TOPIC = "/brain/chat_out"

GET_SERVICE = "/brain/people/get"
RENAME_SERVICE = "/brain/people/rename"
MERGE_SERVICE = "/brain/people/merge"
FORGET_SERVICE = "/brain/people/forget"
SET_COLLECTION_SERVICE = "/brain/people/set_collection"

SNAPSHOT_HEARTBEAT_SEC = 5.0
"""The brain reads a snapshot older than 10 s as a claim about nobody; half
that is the heartbeat that keeps an idle node's health readable."""

LOST_GRACE_SEC = 60.0  # a lost track rides the snapshot this long, as "just left view"
NATIVE_SKEW_NS = 130_000_000
"""How far a native buffer's stamp may sit from the published frame's and still be
the same capture: two frames at 15 fps. The driver's own kMaxNativeSkewNs is a
wider PTS-reset guard, not this — a buffer further out than this is a different
moment, and cropping a face out of it would move the face."""
TALKING_RANGE_M = 3.0
SPEAKING_HOLD_SEC = 3.0  # how long one chat_in message keeps a track marked as the speaker
HINT_TTL_SEC = 120.0  # a disambiguation question ages out with the exchange it came from
ENROLLING_SEC = 60.0  # how long after enrolment a track still counts as "enrolling" to the scribe
SEEK_MIN_RANGE_M = 1.5  # closer than this, walking over gains nothing the head tilt cannot
IDLE_POLL_SEC = 0.5
TICK_POLL_SEC = 0.05
SCRIBE_TICK_SEC = 2.0
STORE_TICK_SEC = 60.0
EXPIRE_EVERY_SEC = 3600.0

IDEMPOTENCY_KEYS = 64
"""How many mutation keys the node answers a retry from. A key is spent within
seconds of being minted, so this is a few minutes of the busiest Settings page."""
DECISION_SKEW_SEC = 1.0
"""How far a track may have started past the frame stamp it was measured on and
still belong to that snapshot: the stamp is the capture, and the tracker's clock
starts when the tick that decoded the frame ran, a camera latency later."""
STALE_DECISION_MESSAGE = "that tag was issued after the snapshot you decided on; look again"

_TAG_RE = re.compile(r"^P\d+$", re.IGNORECASE)

SNAPSHOT_QOS = QoSProfile(
    depth=1,
    history=QoSHistoryPolicy.KEEP_LAST,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)
EVENTS_QOS = QoSProfile(depth=10, history=QoSHistoryPolicy.KEEP_LAST, reliability=QoSReliabilityPolicy.RELIABLE)
SENSOR_QOS = QoSProfile(depth=1, history=QoSHistoryPolicy.KEEP_LAST, reliability=QoSReliabilityPolicy.BEST_EFFORT)
LATCHED_QOS = QoSProfile(
    depth=1,
    history=QoSHistoryPolicy.KEEP_LAST,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


class PeopleHealthDict(HealthDict, total=False):
    """The engine's health plus the scribe's, which has no seat in the shared
    ``HealthDict`` yet (see the report): "none" when it is switched off,
    "unavailable" without a Gemini transport, "stale" while windows queue."""

    scribe: str


class TickSource(StrEnum):
    """Which left-eye topic the engine ticks on; wire-visible (a parameter)."""

    COMPRESSED = "compressed"
    RAW = "raw"


# =========================================================== pure: parameters


@dataclass(frozen=True)
class PeopleNodeConfig:
    """The node's ROS parameters as plain data."""

    enabled: bool = True
    always_on: bool = False
    seek_faces: bool = False
    scribe: bool = True
    prefer_backend: str = "opencv"
    tick_source: str = TickSource.COMPRESSED
    allow_model_download: bool = True
    retention_unnamed_days: float = 14.0
    retention_named_days: float = 548.0
    simulator_mode: bool = False
    camera_height_m: float = 0.26
    gemini_model: str = "gemini-3.6-flash"

    @property
    def data_dir(self) -> Path:
        """Sim evidence never mixes with hardware evidence (RFC 6.1)."""
        return get_innate_os_root() / "data" / ("people_sim" if self.simulator_mode else "people")

    @property
    def models_dir(self) -> Path:
        return get_innate_os_root() / "data" / "models" / "people"

    @property
    def image_topic(self) -> str:
        return RAW_IMAGE_TOPIC if self.tick_source == TickSource.RAW else COMPRESSED_IMAGE_TOPIC


PARAM_DEFAULTS: dict[str, bool | str | float] = {
    "enabled": True,
    "always_on": False,
    "seek_faces": False,
    "scribe": True,
    "prefer_backend": "opencv",
    "tick_source": str(TickSource.COMPRESSED),
    # True so a robot provisioned without the model files still recognizes a
    # face after one fetch; provisioning is meant to ship them under
    # data/models/people, and an appliance that cannot reach the internet
    # degrades to detection either way.
    "allow_model_download": True,
    "retention_unnamed_days": 14.0,
    "retention_named_days": 548.0,
    "simulator_mode": False,
    "camera_height_m": 0.26,
    "gemini_model": "gemini-3.6-flash",
}


def config_from_params(values: Mapping[str, Any]) -> PeopleNodeConfig:
    """The declared parameter values as a config. A value of the wrong type
    keeps the default: a mistyped setting must not stop the node from seeing."""
    fields: dict[str, Any] = {}
    for name, default in PARAM_DEFAULTS.items():
        value = values.get(name, default)
        if isinstance(default, bool):
            fields[name] = bool(value)
        elif isinstance(default, float):
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            fields[name] = float(value) if numeric else default
        else:
            fields[name] = value if isinstance(value, str) and value else default
    if fields["tick_source"] not in (TickSource.COMPRESSED, TickSource.RAW):
        fields["tick_source"] = str(TickSource.COMPRESSED)
    return PeopleNodeConfig(**fields)


# =============================================================== pure: frames


@dataclass(frozen=True)
class CameraFrame:
    """One left-eye frame as it arrived, decoded only once the engine wants it."""

    stamp_ns: int
    data: bytes
    encoding: str = "jpeg"  # "jpeg" | "bgr8" | "rgb8"
    width: int = 0
    height: int = 0


def stamp_ns(sec: int, nanosec: int) -> int:
    """A ROS header stamp as the integer nanoseconds every consumer names a
    frame by (the brain pairs its overlay on an exact match)."""
    return sec * 10**9 + nanosec


def stamp_text(value: int | None) -> str | None:
    """``frame_stamp_ns`` rides the snapshot as a decimal string: a JSON number
    loses the last digits of a nanosecond stamp in a JavaScript consumer."""
    return None if value is None else str(value)


def decode_frame(frame: CameraFrame) -> np.ndarray | None:
    """The frame as BGR pixels, or None when it is unreadable."""
    if frame.encoding == "jpeg":
        return native_frames.decode(frame.data)
    if frame.encoding not in ("bgr8", "rgb8"):
        return None
    pixels = frame.width * frame.height * 3
    if pixels <= 0 or len(frame.data) < pixels:
        return None
    image = np.frombuffer(frame.data, dtype=np.uint8, count=pixels).reshape(frame.height, frame.width, 3)
    # frombuffer is read-only and an rgb8 flip is a negative-stride view; cv2
    # refuses both, so the copy is the price of not owning the message memory.
    return np.array(image if frame.encoding == "bgr8" else image[:, :, ::-1], dtype=np.uint8, order="C")


def pair_native(
    frame_stamp: int, buffers: Sequence[tuple[int, bytes]], *, max_skew_ns: int = NATIVE_SKEW_NS
) -> bytes | None:
    """The native MJPG buffer captured with this published frame. The driver
    stamps the buffer with the published frame's stamp plus the PTS delta, so
    the nearest stamp inside the driver's own skew cap is the right one."""
    best: tuple[int, bytes] | None = None
    for stamp, data in buffers:
        skew = abs(stamp - frame_stamp)
        if skew > max_skew_ns:
            continue
        if best is None or skew < abs(best[0] - frame_stamp):
            best = (stamp, data)
    return best[1] if best is not None else None


# =========================================================== pure: duty cycle


def engine_active(*, enabled: bool, always_on: bool, brain_active: bool) -> bool:
    """Whether the engine should be looking at all (RFC 3.1): the brain's
    lifecycle drives it unless the owner asked for "always on"."""
    return enabled and (always_on or brain_active)


def decode_period(config: EngineConfig, *, tracked: bool, driving: bool, motion: bool) -> float:
    """How often the node decodes a frame for the engine — the engine's own
    detect cadence (RFC 4.6). Decoding faster only throws JPEGs away."""
    if driving:
        return 1.0 / config.driving_detect_hz
    if tracked or motion:
        return 1.0 / config.active_detect_hz
    return 1.0 / config.idle_detect_hz


def wants_native(tracks: Sequence[TrackState], now: float, *, refresh_sec: float = 5.0) -> bool:
    """Whether any live track still needs the sensor's own pixels: everyone
    unsettled, and a settled track whose last face is older than the refresh
    interval. False unsubscribes the lazy native topic, which is what makes it
    free in the driver."""
    for track in tracks:
        if track.lost:
            continue
        if track.identity.state not in (IdentityState.KNOWN, IdentityState.FAMILIAR):
            return True
        if track.last_face_stamp is None or now - track.last_face_stamp >= refresh_sec:
            return True
    return False


# ============================================================ pure: snapshots


def publishable_tracks(
    tracks: Sequence[TrackState], now: float, *, grace_sec: float = LOST_GRACE_SEC
) -> list[TrackState]:
    """Live tracks, plus the ones lost recently enough to still be worth saying
    "just left view" about; the tracker remembers lost tracks for five minutes,
    far longer than the brain wants to hear about them."""
    return [track for track in tracks if not track.lost or now - track.last_seen <= grace_sec]


def fresh_tracks(
    tracks: Sequence[TrackState], *, now: float, last_frame_at: float, stale_sec: float
) -> tuple[TrackState, ...]:
    """The tracks a silent camera is still allowed to claim: none. The tracker
    ages a track out on the tick that misses it, so without frames the scene
    would freeze with everybody still in it (RFC section 9)."""
    if now - last_frame_at > stale_sec:
        return ()
    return tuple(tracks)


def apply_conflicts(tracks: Sequence[TrackState], resolutions: Mapping[str, Resolution]) -> list[TrackState]:
    """One person cannot be two live tracks (RFC 5.3.4). The resolver reports
    the clash and keeps the loser's belief; the snapshot has to *say* it, so
    the track reads ``conflict`` — person and name kept, runner-up dropped,
    since the rival identity is itself and not a second candidate."""
    resolved: list[TrackState] = []
    for track in tracks:
        resolution = resolutions.get(track.tag)
        if resolution is None or resolution.conflict_with is None:
            resolved.append(track)
            continue
        identity = replace(track.identity, state=IdentityState.CONFLICT, runner_up_name=None)
        resolved.append(replace(track, identity=identity))
    return resolved


def seek_hint(
    attention: AttentionDict | None, tracks: Sequence[TrackState], *, seek_faces: bool
) -> AttentionDict | None:
    """With ``seek_faces`` on, the attention line names the skill that would get
    the face (RFC 5.5: approach is opt-in, and it stays a nudge in the block —
    the node never calls a tool)."""
    if attention is None or not seek_faces:
        return attention
    tag = attention.get("tag") or ""
    target = next((track for track in tracks if track.tag == tag), None)
    if target is None or target.frames_with_face > 0:
        return attention
    if target.range_m is not None and target.range_m <= SEEK_MIN_RANGE_M:
        return attention
    return {**attention, "text": f"{attention.get('text', '')}; approach_person({tag}) would get a look"}


def snapshot_changed(current: PeopleSnapshotDict, previous: PeopleSnapshotDict | None) -> bool:
    """Whether anything but the clock moved: an idle scene must not republish a
    kilobyte of identical JSON five times a second."""
    if previous is None:
        return True
    volatile = ("stamp", "frame_stamp_ns")
    return {key: value for key, value in current.items() if key not in volatile} != {
        key: value for key, value in previous.items() if key not in volatile
    }


def should_publish(
    current: PeopleSnapshotDict,
    previous: PeopleSnapshotDict | None,
    *,
    now: float,
    last_publish: float,
    active: bool,
    heartbeat_sec: float = SNAPSHOT_HEARTBEAT_SEC,
) -> bool:
    """Every tick while anyone is in view, on any change otherwise, and at least
    every ``heartbeat_sec`` so health stays readable."""
    return active or now - last_publish >= heartbeat_sec or snapshot_changed(current, previous)


def held_speaking(pending: tuple[str, float] | None, now: float) -> tuple[str, ...]:
    """The tag a recent chat_in message was attributed to, while it lasts."""
    if pending is None or now >= pending[1]:
        return ()
    return (pending[0],)


def speaking_tag(tracks: Sequence[TrackState], *, max_range_m: float = TALKING_RANGE_M) -> str | None:
    """Who just spoke, when the answer is not a guess: the one live track in
    talking range. Two candidates or none attribute to nobody — the scribe's
    name rules refuse to commit on a guess, and so does this."""
    candidates = [
        track for track in tracks if not track.lost and (track.range_m is None or track.range_m <= max_range_m)
    ]
    return candidates[0].tag if len(candidates) == 1 else None


# =============================================================== pure: scribe


def tag_views(tracks: Sequence[TrackState], enrolling: Iterable[str] = ()) -> tuple[TagView, ...]:
    """The live tracks as the scribe sees them when a message arrives."""
    fresh = set(enrolling)
    return tuple(
        TagView(
            tag=track.tag,
            state=track.identity.state,
            person_id=track.identity.person_id,
            name=track.identity.name,
            enrolling=track.tag in fresh,
        )
        for track in tracks
        if not track.lost
    )


def chat_in_utterance(
    payload: Mapping[str, Any], *, uid: str, now: float, in_view: Sequence[TagView]
) -> Utterance | None:
    """A ``/brain/chat_in`` entry as one transcript line, or None when it is not
    a person talking to the robot. Simulated environment speech is dropped: it
    comes out of the robot's own speaker on behalf of a scripted resident, so it
    is neither the user in view nor the robot's own words, and a transcript
    claiming either would teach the scribe a lie."""
    if payload.get("sender") == "environment_speech":
        return None
    text = str(payload.get("text") or "").strip()
    if not text:
        return None
    return Utterance(id=uid, stamp=now, speaker=Speaker.USER, text=text, in_view=tuple(in_view))


def chat_out_utterance(
    payload: Mapping[str, Any], *, uid: str, now: float, in_view: Sequence[TagView]
) -> Utterance | None:
    """A ``/brain/chat_out`` entry as one transcript line. Only what the robot
    said out loud and what a skill reported count; thoughts and system notes
    were never spoken, and would read to the scribe as speech."""
    if payload.get("sender") not in ("robot", "skill_output"):
        return None
    text = str(payload.get("text") or "").strip()
    if not text:
        return None
    return Utterance(id=uid, stamp=now, speaker=Speaker.ROBOT, text=text, in_view=tuple(in_view))


@dataclass(frozen=True)
class Recall:
    """What a deep recall came back with, on its way from the scribe thread to
    the engine thread. It crosses as data because :class:`PeopleEvents` reads
    then writes its cooldown dicts, and only the engine thread may touch it."""

    person_id: str
    name: str | None
    text: str
    stamp: float


def recall_person(subject: str, tracks: Sequence[TrackState], roster: Sequence[RosterEntryDict]) -> str | None:
    """Who a memory question is about (RFC 6.4). ``is_memory_question`` hands
    back a name, a ``P<n>`` tag or a bare pronoun; a pronoun resolves against
    who is in view, and only when exactly one person is."""
    if _TAG_RE.match(subject):
        track = next((t for t in tracks if t.tag.upper() == subject.upper() and not t.lost), None)
        return track.identity.person_id if track is not None else None
    wanted = subject.strip().casefold()
    for entry in roster:
        if (entry.get("name") or "").strip().casefold() == wanted and wanted:
            return entry.get("person_id")
    in_view = {track.identity.person_id for track in tracks if not track.lost and track.identity.person_id}
    return in_view.pop() if len(in_view) == 1 else None


# ============================================================= pure: services


@dataclass(frozen=True)
class MutationResult:
    """What a mutation answered, kept so a retry can be answered the same way.
    ``person_id`` is empty for the services whose response has no such field."""

    success: bool
    message: str
    person_id: str = ""


class MutationLog:
    """The answers the last :data:`IDEMPOTENCY_KEYS` idempotency keys got.

    A call carrying a key this node has already answered is a retry — a reply
    the caller never received, a second tap on the Settings page — and RFC
    section 8 answers it from here rather than merging two people twice. An
    empty key is no key: those calls are always fresh mutations.
    """

    def __init__(self, limit: int = IDEMPOTENCY_KEYS) -> None:
        self._limit = limit
        self._answers: dict[str, MutationResult] = {}

    def answered(self, key: str) -> MutationResult | None:
        return self._answers.get(key.strip()) if key.strip() else None

    def remember(self, key: str, result: MutationResult) -> None:
        key = key.strip()
        if not key:
            return
        self._answers.pop(key, None)  # re-inserted last, so a live key is not the one evicted
        self._answers[key] = result
        while len(self._answers) > self._limit:
            del self._answers[next(iter(self._answers))]


def tag_newer_than_decision(who: str, tracks: Sequence[TrackState], decided_on_stamp_ns: str) -> bool:
    """Whether ``who`` names a live tag whose track started after the snapshot
    the caller decided on (RFC section 8). A caller acting on a tag its snapshot
    never carried is answering about somebody it has not seen, so the mutation
    is refused instead of landing on whoever holds that tag now."""
    decided_on = _stamp_seconds(decided_on_stamp_ns)
    query = who.strip()
    if decided_on is None or not _TAG_RE.match(query):
        return False
    track = next((t for t in tracks if t.tag.upper() == query.upper()), None)
    return track is not None and track.first_seen > decided_on + DECISION_SKEW_SEC


def _stamp_seconds(stamp_ns: str) -> float | None:
    """``frame_stamp_ns`` as epoch seconds; None when there is no stamp to check."""
    try:
        return int(stamp_ns) / 1e9
    except (TypeError, ValueError):
        return None


def resolve_who(
    who: str,
    tracks: Sequence[TrackState],
    roster: Sequence[RosterEntryDict],
    *,
    forgotten: Callable[[str], bool] = lambda _person_id: False,
) -> tuple[str | None, str]:
    """``who`` — a live tag, a person id, or a name — as a person id, or None
    with a message the caller can say out loud. A tag that has expired, or a
    name two people answer to, is an error: never the nearest live track."""
    query = who.strip()
    if not query:
        return None, "no person given: pass a live tag like P3, or a person id"
    if _TAG_RE.match(query):
        track = next((t for t in tracks if t.tag.upper() == query.upper()), None)
        if track is None:
            return None, f"{query} is not a track I have any more — try again while they are in view"
        if track.identity.person_id is None:
            return None, f"{query} is not somebody I have on file yet"
        return track.identity.person_id, ""
    if any(entry.get("person_id") == query for entry in roster):
        return query, ""
    if query.startswith("person_"):
        return None, f"I have nobody with the id {query}" + (" — that one was forgotten" if forgotten(query) else "")
    matches = [entry for entry in roster if (entry.get("name") or "").strip().casefold() == query.casefold()]
    if len(matches) == 1:
        return matches[0].get("person_id"), ""
    if matches:
        return None, f"I know {len(matches)} people called {query} — say which one by their id"
    return None, f"I do not know anybody called {query}"


def roster_answer(
    snapshot: PeopleSnapshotDict, *, roster: Sequence[RosterEntryDict] | None, capacity_full: bool
) -> PeopleRosterDict:
    """``GetPeople``'s payload: the live snapshot, plus the roster when it was
    asked for. A missing ``roster`` key reads as "this node is too old" in the
    Settings page, so it is present exactly when it was requested."""
    answer = cast("PeopleRosterDict", dict(snapshot))
    if roster is None:
        return answer
    answer["roster"] = list(roster)
    answer["capacity_full"] = capacity_full
    return answer


def empty_snapshot(health: HealthDict, now: float, *, collection_enabled: bool) -> PeopleSnapshotDict:
    """What the node latches before its first tick, and while it is idle — an
    honest "nobody, and here is the state of the sensors" rather than silence."""
    return {
        "schema": SNAPSHOT_SCHEMA,
        "stamp": round(now, 3),
        "frame_stamp_ns": None,
        "image_size": [640, 480],
        "health": health,
        "collection_enabled": collection_enabled,
        "attention": None,
        "people": [],
        "recent": [],
    }


# ============================================================== ROS adapters


class PeopleSensors:
    """Every subscription the engine reads. Callbacks copy into slots under one
    lock and return; the camera subscriptions exist only while the engine runs
    and are created and destroyed on the executor thread (see the module
    docstring) through :meth:`set_camera_enabled` and :meth:`set_native_enabled`."""

    def __init__(
        self,
        node: Node,
        config: PeopleNodeConfig,
        *,
        on_camera: Callable[[CameraModel], None],
        motion_burst_sec: float,
    ):
        self._node = node
        self._config = config
        self._on_camera = on_camera
        self._motion = MotionGate()  # the brain's gate, on this node's own stream (RFC 4.6)
        self._motion_burst_sec = motion_burst_sec
        self._motion_until = 0.0
        self._lock = threading.Lock()
        self._frame: CameraFrame | None = None
        self._natives: deque[tuple[int, bytes]] = deque(maxlen=4)
        self._ego = EgoMotionTracker()
        self._pose: Pose | None = None
        self._map_name: str | None = None
        self._intrinsics: tuple[float, ...] | None = None
        self._image_sub: Subscription | None = None
        self._info_sub: Subscription | None = None
        self._native_sub: Subscription | None = None
        self.brain_active = False

        node.create_subscription(String, HEAD_TOPIC, self._on_head, 10)
        node.create_subscription(Twist, CMD_VEL_TOPIC, self._on_cmd_vel, 10)
        node.create_subscription(Odometry, ODOM_TOPIC, self._on_odom, SENSOR_QOS)
        node.create_subscription(PoseWithCovarianceStamped, AMCL_POSE_TOPIC, self._on_amcl_pose, LATCHED_QOS)
        node.create_subscription(String, CURRENT_MAP_TOPIC, self._on_current_map, 10)
        node.create_subscription(String, AGENT_STATUS_TOPIC, self._on_agent_status, LATCHED_QOS)

    # --- lifecycle (executor thread only) ---

    def set_camera_enabled(self, wanted: bool) -> None:
        if wanted == (self._image_sub is not None):
            return
        if not wanted:
            for subscription in (self._image_sub, self._info_sub):
                if subscription is not None:
                    self._node.destroy_subscription(subscription)
            self._image_sub = self._info_sub = None
            self._motion = MotionGate()  # a restart must not diff against pre-stop frames
            with self._lock:
                self._frame = None
            return
        if self._config.tick_source == TickSource.RAW:
            self._image_sub = self._node.create_subscription(
                Image, self._config.image_topic, self._on_raw_image, SENSOR_QOS
            )
        else:
            self._image_sub = self._node.create_subscription(
                CompressedImage, self._config.image_topic, self._on_compressed_image, SENSOR_QOS
            )
        self._info_sub = self._node.create_subscription(CameraInfo, CAMERA_INFO_TOPIC, self._on_camera_info, SENSOR_QOS)

    def set_native_enabled(self, wanted: bool) -> None:
        """The native topic is lazy in the driver: it costs a tee pull only
        while somebody subscribes, so the subscription itself is the throttle."""
        if wanted == (self._native_sub is not None):
            return
        if not wanted:
            if self._native_sub is not None:
                self._node.destroy_subscription(self._native_sub)
            self._native_sub = None
            with self._lock:
                self._natives.clear()
            return
        self._native_sub = self._node.create_subscription(CompressedImage, NATIVE_TOPIC, self._on_native, SENSOR_QOS)

    # --- reads (engine thread) ---

    def take_frame(self) -> CameraFrame | None:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame

    def native_for(self, frame_stamp: int) -> bytes | None:
        with self._lock:
            return pair_native(frame_stamp, list(self._natives))

    def ego(self, now: float) -> EgoMotion:
        with self._lock:
            return self._ego.state(now)

    def scene(self) -> tuple[str | None, Pose | None]:
        with self._lock:
            return self._map_name, self._pose

    def motion(self, now: float) -> bool:
        """Whether the scene changed recently enough to be worth looking at
        properly — the burst of RFC 4.6, off the same gate the brain wakes on."""
        return now < self._motion_until

    # --- callbacks ---

    def _on_compressed_image(self, msg: CompressedImage) -> None:
        if not msg.data:
            return
        data = bytes(msg.data)
        with self._lock:
            self._frame = CameraFrame(stamp_ns(msg.header.stamp.sec, msg.header.stamp.nanosec), data)
            ego = self._ego.state(time.time())
        # Outside the lock: the gate decodes, and the engine thread must never
        # wait on a decode to read the frame it is about to work on.
        if self._motion.observe(data, ego.head_pitch_deg, ego.recently_driven):
            self._motion_until = time.time() + self._motion_burst_sec

    def _on_raw_image(self, msg: Image) -> None:
        if not msg.data:
            return
        with self._lock:
            self._frame = CameraFrame(
                stamp_ns(msg.header.stamp.sec, msg.header.stamp.nanosec),
                bytes(msg.data),
                encoding=msg.encoding or "bgr8",
                width=msg.width,
                height=msg.height,
            )

    def _on_native(self, msg: CompressedImage) -> None:
        if not msg.data:
            return
        with self._lock:
            self._natives.append((stamp_ns(msg.header.stamp.sec, msg.header.stamp.nanosec), bytes(msg.data)))

    def _on_camera_info(self, msg: CameraInfo) -> None:
        intrinsics = tuple(float(value) for value in msg.k)
        if intrinsics == self._intrinsics:
            return
        self._intrinsics = intrinsics
        self._on_camera(
            CameraModel.from_camera_info(
                intrinsics, msg.width, msg.height, tuple(float(value) for value in msg.d), self._config.camera_height_m
            )
        )

    def _on_head(self, msg: String) -> None:
        try:
            pitch = float(json.loads(msg.data)["current_position"])
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            return  # keep the last known pitch: it drives the floor-ray range
        with self._lock:
            self._ego.note_head_pitch(time.time(), pitch)

    def _on_cmd_vel(self, msg: Twist) -> None:
        with self._lock:
            self._ego.note_cmd_vel(time.time(), msg.linear.x, msg.linear.y, msg.angular.z)

    def _on_odom(self, msg: Odometry) -> None:
        with self._lock:
            self._ego.note_yaw_rate(time.time(), msg.twist.twist.angular.z)

    def _on_amcl_pose(self, msg: PoseWithCovarianceStamped) -> None:
        pose = msg.pose.pose
        with self._lock:
            self._pose = (pose.position.x, pose.position.y, quaternion_to_yaw(pose.orientation))

    def _on_current_map(self, msg: String) -> None:
        with self._lock:
            self._map_name = msg.data or None

    def _on_agent_status(self, msg: String) -> None:
        try:
            self.brain_active = bool(json.loads(msg.data).get("brain_active", False))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return  # an unreadable status must not silently blind the engine


class PeopleAdapters:
    """The node's moving parts: the engine thread that ticks and publishes, the
    scribe thread that spends the Gemini calls, the five services, and the slow
    timer that flushes and expires the store."""

    def __init__(
        self,
        node: Node,
        config: PeopleNodeConfig,
        *,
        store: PeopleStore,
        engine: PeopleEngine,
        engine_config: EngineConfig,
        scribe: Scribe | None,
        transport: Transport | None,
    ):
        self._node = node
        self._logger = node.get_logger()
        self._config = config
        self._store = store
        self._engine = engine
        self._engine_config = engine_config
        self._scribe = scribe
        self._transport = transport
        self._events = PeopleEvents()
        self._mutations = MutationLog()
        self._sensors = PeopleSensors(
            node, config, on_camera=engine.set_camera, motion_burst_sec=engine_config.motion_burst_sec
        )

        self._lock = threading.Lock()
        self._tracks: tuple[TrackState, ...] = ()
        self._snapshot: PeopleSnapshotDict | None = None
        self._published: PeopleSnapshotDict | None = None
        self._last_publish = 0.0
        self._last_tick = 0.0
        self._learned: dict[str, str] = {}
        self._hints: dict[str, tuple[str, float]] = {}
        self._enrolling: dict[str, float] = {}
        self._speaking: tuple[str, float] | None = None
        self._pending_recalls: deque[Recall] = deque(maxlen=32)
        self._want_native = False
        self._uid = 0
        self._expired_at = time.monotonic()
        self._last_frame_at = 0.0

        self._chat: queue.Queue[Utterance] = queue.Queue(maxsize=64)
        self._forgets: queue.Queue[str] = queue.Queue(maxsize=8)
        self._suppressions: queue.Queue[str] = queue.Queue(maxsize=8)
        self._rebinds: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=8)
        self._recalls: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=8)
        self._descriptions: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=8)

        self._snapshot_pub: Publisher = node.create_publisher(String, SNAPSHOT_TOPIC, SNAPSHOT_QOS)
        self._events_pub: Publisher = node.create_publisher(String, EVENTS_TOPIC, EVENTS_QOS)
        node.create_subscription(String, CHAT_IN_TOPIC, self._on_chat_in, 10)
        node.create_subscription(String, CHAT_OUT_TOPIC, self._on_chat_out, 10)
        self._create_services()

        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._engine_loop, name="people_engine", daemon=True)]
        if scribe is not None:
            self._threads.append(threading.Thread(target=self._scribe_loop, name="people_scribe", daemon=True))
        node.create_timer(1.0, self._activation_tick)
        node.create_timer(STORE_TICK_SEC, self._store_tick)

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._store.set_next_tag(self._engine.tracker.next_tag)
        self._store.flush()

    def tracks(self) -> tuple[TrackState, ...]:
        """Who the engine is tracking right now — empty once the camera has
        gone quiet, so a service never resolves a tag against a frozen scene."""
        with self._lock:
            tracks = self._tracks
        return fresh_tracks(
            tracks,
            now=time.time(),
            last_frame_at=self._last_frame_at,
            stale_sec=self._engine_config.camera_stale_sec,
        )

    # ------------------------------------------------------ executor thread

    def _activation_tick(self) -> None:
        active = engine_active(
            enabled=self._config.enabled, always_on=self._config.always_on, brain_active=self._sensors.brain_active
        )
        self._sensors.set_camera_enabled(active)
        self._sensors.set_native_enabled(active and self._want_native)

    def _store_tick(self) -> None:
        try:
            self._store.set_next_tag(self._engine.tracker.next_tag)
            self._store.flush()
            if time.monotonic() - self._expired_at >= EXPIRE_EVERY_SEC:
                self._expired_at = time.monotonic()
                for person_id in self._store.expire(time.time()):
                    self._logger.info(f"[People] {person_id} expired past its retention window")
        except OSError as error:
            self._logger.error(f"[People] store flush failed: {error!r}")

    def _create_services(self) -> None:
        self._node.create_service(GetPeople, GET_SERVICE, self._svc_get)
        self._node.create_service(RenamePerson, RENAME_SERVICE, self._svc_rename)
        self._node.create_service(MergePeople, MERGE_SERVICE, self._svc_merge)
        self._node.create_service(ForgetPerson, FORGET_SERVICE, self._svc_forget)
        self._node.create_service(SetPeopleCollection, SET_COLLECTION_SERVICE, self._svc_set_collection)

    def _svc_get(self, request: GetPeople.Request, response: GetPeople.Response) -> GetPeople.Response:
        now = time.time()
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            snapshot = empty_snapshot(self._health(now), now, collection_enabled=self._store.collection_enabled())
        roster = self._store.roster(include_thumbnails=request.include_thumbnails) if request.include_roster else None
        response.success = True
        response.message = ""
        response.json = json.dumps(roster_answer(snapshot, roster=roster, capacity_full=self._store.capacity_full()))
        return response

    def _svc_rename(self, request: RenamePerson.Request, response: RenamePerson.Response) -> RenamePerson.Response:
        result = self._mutate(request.idempotency_key, lambda: self._rename(request))
        response.success, response.message, response.person_id = result.success, result.message, result.person_id
        return response

    def _svc_merge(self, request: MergePeople.Request, response: MergePeople.Response) -> MergePeople.Response:
        result = self._mutate(request.idempotency_key, lambda: self._merge(request))
        response.success, response.message = result.success, result.message
        return response

    def _svc_forget(self, request: ForgetPerson.Request, response: ForgetPerson.Response) -> ForgetPerson.Response:
        result = self._mutate(request.idempotency_key, lambda: self._forget(request))
        response.success, response.message = result.success, result.message
        return response

    def _svc_set_collection(
        self, request: SetPeopleCollection.Request, response: SetPeopleCollection.Response
    ) -> SetPeopleCollection.Response:
        response.success, response.message = self._write(
            lambda: self._store.set_collection(request.enabled), "could not change the collection setting"
        )
        return response

    def _rename(self, request: RenamePerson.Request) -> MutationResult:
        stale = self._stale(request.decided_on_stamp_ns, request.who)
        if stale is not None:
            return stale
        person_id, message = self._resolve(request.who)
        name = request.name.strip()
        if person_id is None or not name:
            return MutationResult(False, message or "a name cannot be empty")
        success, message = self._write(
            lambda: self._store.rename(person_id, name, request.source or "app"), f"could not rename {person_id}"
        )
        if success:
            # The name is committed by now, so a failure behind it is logged
            # rather than reported: the rename did happen.
            self._write(
                lambda: self._store.clear_name_candidates(person_id), f"could not clear {person_id}'s candidates"
            )
        return MutationResult(success, message, person_id)

    def _merge(self, request: MergePeople.Request) -> MutationResult:
        stale = self._stale(request.decided_on_stamp_ns, request.source_id, request.target_id)
        if stale is not None:
            return stale
        source, source_message = self._resolve(request.source_id)
        target, target_message = self._resolve(request.target_id)
        if source is None or target is None:
            return MutationResult(False, source_message or target_message)
        success, message = self._write(lambda: self._store.merge(source, target), "those two cannot be merged")
        if success:
            # The resolver is the engine thread's, like the forget suppressions.
            _offer(self._rebinds, (source, target))
        return MutationResult(success, message)

    def _forget(self, request: ForgetPerson.Request) -> MutationResult:
        stale = self._stale(request.decided_on_stamp_ns, request.who)
        if stale is not None:
            return stale
        person_id, message = self._resolve(request.who)
        if person_id is None:
            return MutationResult(False, message)
        success, message = self._write(lambda: self._store.forget(person_id), f"could not forget {person_id}")
        if success:
            self._suppress(person_id)
            # The queue and the open window are the scribe thread's; handing it
            # the id keeps this off the executor and off its file (RFC 10).
            _offer(self._forgets, person_id)
        return MutationResult(success, message)

    def _mutate(self, idempotency_key: str, write: Callable[[], MutationResult]) -> MutationResult:
        answered = self._mutations.answered(idempotency_key)
        if answered is not None:
            return answered
        result = write()
        self._mutations.remember(idempotency_key, result)
        return result

    def _stale(self, decided_on_stamp_ns: str, *who: str) -> MutationResult | None:
        tracks = self.tracks()
        if any(tag_newer_than_decision(one, tracks, decided_on_stamp_ns) for one in who):
            return MutationResult(False, STALE_DECISION_MESSAGE)
        return None

    def _write(self, mutate: Callable[[], bool | None], failure: str) -> tuple[bool, str]:
        """A store write, answered either way: a full disk must reach the
        Settings page as a message, not as a call that never comes back. A
        write with nothing to report answers None and counts as done."""
        try:
            return (False, failure) if mutate() is False else (True, "")
        except OSError as error:
            self._logger.error(f"[People] {failure}: {error!r}")
            return False, failure

    def _resolve(self, who: str) -> tuple[str | None, str]:
        return resolve_who(who, self.tracks(), self._store.roster(), forgotten=self._store.is_tombstoned)

    def _suppress(self, person_id: str) -> None:
        """A forgotten person must stop being that person on the live track, or
        the next tick names an id that no longer exists. The resolver is the
        engine thread's, so the tag crosses to it and :meth:`_tick` applies it."""
        for track in self.tracks():
            if track.identity.person_id == person_id:
                _offer(self._suppressions, track.tag)

    def _on_chat_in(self, msg: String) -> None:
        payload = _payload(msg.data)
        if payload is None:
            return
        now = time.time()
        tracks = self.tracks()
        utterance = chat_in_utterance(payload, uid=self._next_uid(), now=now, in_view=self._views(tracks, now))
        if utterance is None:
            return
        tag = speaking_tag(tracks)
        if tag is not None:
            with self._lock:
                self._speaking = (tag, now + SPEAKING_HOLD_SEC)
        _offer(self._chat, utterance)
        self._maybe_recall(utterance.text, tracks)

    def _on_chat_out(self, msg: String) -> None:
        payload = _payload(msg.data)
        if payload is None:
            return
        now = time.time()
        views = self._views(self.tracks(), now)
        utterance = chat_out_utterance(payload, uid=self._next_uid(), now=now, in_view=views)
        if utterance is not None:
            _offer(self._chat, utterance)

    def _maybe_recall(self, text: str, tracks: Sequence[TrackState]) -> None:
        if self._scribe is None:
            return
        roster = self._store.roster()
        subject = is_memory_question(text, [entry.get("name") or "" for entry in roster])
        if subject is None:
            return
        person_id = recall_person(subject, tracks, roster)
        if person_id is not None:
            _offer(self._recalls, (person_id, text))

    def _next_uid(self) -> str:
        with self._lock:
            self._uid += 1
            return f"u_{self._uid}"

    def _views(self, tracks: Sequence[TrackState], now: float) -> tuple[TagView, ...]:
        with self._lock:
            self._enrolling = {tag: at for tag, at in self._enrolling.items() if now - at <= ENROLLING_SEC}
            enrolling = list(self._enrolling)
        return tag_views(tracks, enrolling)

    # -------------------------------------------------------- engine thread

    def _engine_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_forever()
            except Exception as error:  # noqa: BLE001 — a crashed loop restarts; the robot must not go blind
                self._logger.error(f"[People] engine loop crashed, restarting in 2s: {error!r}")
                self._stop.wait(2.0)

    def _tick_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick(time.time())
            except Exception as error:  # noqa: BLE001 — the loop must outlive one bad frame
                self._logger.error(f"[People] tick failed: {error!r}", throttle_duration_sec=5.0)
            self._stop.wait(TICK_POLL_SEC)

    def _tick(self, now: float) -> None:
        self._apply_suppressions()
        self._apply_rebinds()
        active = engine_active(
            enabled=self._config.enabled, always_on=self._config.always_on, brain_active=self._sensors.brain_active
        )
        if not active:
            self._want_native = False
            self._publish_snapshot(now, (), None, (640, 480), consume=False)
            self._stop.wait(IDLE_POLL_SEC)
            return
        ego = self._sensors.ego(now)
        motion = self._sensors.motion(now)
        # Live tracks only: a lost one lingers for five minutes so it can be
        # re-associated, and counting it would hold the duty cycle at 5 Hz for
        # those five minutes with nobody in the room (RFC 4.6).
        period = decode_period(
            self._engine_config,
            tracked=any(not track.lost for track in self._engine.tracks()),
            driving=ego.recently_driven,
            motion=motion,
        )
        if now - self._last_tick < period:
            return
        self._last_tick = now
        frame = self._sensors.take_frame()
        image = decode_frame(frame) if frame is not None else None
        if frame is None or image is None:
            if frame is not None:
                self._logger.warn("[People] undecodable camera frame", throttle_duration_sec=10.0)
            self._publish_snapshot(now, self.tracks(), None, (640, 480), consume=False)
            return
        self._last_frame_at = now
        native = self._sensors.native_for(frame.stamp_ns) if self._want_native else None
        map_name, pose = self._sensors.scene()
        with self._lock:
            speaking = held_speaking(self._speaking, now)
        tracks = self._engine.tick(
            image,
            native,
            now,
            ego,
            None,
            motion=motion,
            map_name=map_name,
            pose=pose,
            speaking=speaking,
            frame_stamp_ns=stamp_text(frame.stamp_ns),
        )
        self._want_native = wants_native(tracks, now, refresh_sec=self._engine_config.face_refresh_sec)
        resolutions = self._engine.resolutions()
        resolved = apply_conflicts(tracks, resolutions)
        with self._lock:
            self._tracks = tuple(resolved)
        self._after_tick(image, resolved, resolutions, now)
        # The engine's stamp, not this frame's: a tick its own duty cycle skipped
        # measured nothing, and the brain draws only on the frame it measured.
        self._publish_snapshot(
            now, resolved, self._engine.frame_stamp_ns, (image.shape[1], image.shape[0]), consume=True
        )

    def _apply_suppressions(self) -> None:
        """The forgotten tags the services queued, dropped from the resolver here
        where nothing else is reading its beliefs."""
        while True:
            try:
                tag = self._suppressions.get_nowait()
            except queue.Empty:
                return
            self._engine.resolver.forget(tag)

    def _apply_rebinds(self) -> None:
        """The merges the services queued: the track keeps its person under the
        id that survived, instead of the tombstone it was committed to."""
        while True:
            try:
                source, target = self._rebinds.get_nowait()
            except queue.Empty:
                return
            self._engine.resolver.rebind(source, target)

    def _after_tick(
        self, image: np.ndarray, tracks: Sequence[TrackState], resolutions: Mapping[str, Resolution], now: float
    ) -> None:
        """What the resolver decided this tick, turned into wake events, an
        enrolment thumbnail, and the one description a new person earns."""
        enrolled: dict[str, bytes | None] = {}
        for tag, resolution in resolutions.items():
            if resolution.switched_from is not None:
                self._logger.info(f"[People] {tag} switched from {resolution.switched_from}")
            if resolution.enrolled_id is None:
                continue
            thumbnails = self._store.thumbnails(resolution.enrolled_id)
            enrolled[tag] = thumbnails[-1] if thumbnails else None
            with self._lock:
                self._enrolling[tag] = now
            self._queue_description(resolution.enrolled_id, image, tracks, tag)
        self._publish_events(tracks, now, enrolled)

    def _queue_description(self, person_id: str, image: np.ndarray, tracks: Sequence[TrackState], tag: str) -> None:
        if self._scribe is None or self._store.description(person_id):
            return
        track = next((t for t in tracks if t.tag == tag), None)
        if track is None:
            return
        crop = description.crop_for_description(native_frames.unsquash_published(image), track.box)
        if crop is not None:
            _offer(self._descriptions, (person_id, crop))

    def _publish_snapshot(
        self,
        now: float,
        tracks: Sequence[TrackState],
        frame_stamp: str | None,
        image_size: tuple[int, int],
        *,
        consume: bool,
    ) -> None:
        """``consume`` clears the scribe's "learned:" lines, and only the tick
        that published their events may do it: a tick with no frame publishes no
        events, so clearing there loses the name the brain never heard about."""
        visible = publishable_tracks(tracks, now)
        learned, hints = self._lines(now)
        attention = seek_hint(choose_attention(visible, now), visible, seek_faces=self._config.seek_faces)
        snapshot = build_snapshot(
            visible,
            self._store,
            self._health(now),
            now,
            frame_stamp_ns=frame_stamp,
            image_size=image_size,
            attention=attention,
            learned=learned,
            hints=hints,
        )
        with self._lock:
            self._snapshot = snapshot
        if not should_publish(
            snapshot, self._published, now=now, last_publish=self._last_publish, active=bool(visible)
        ):
            return
        self._published = snapshot
        self._last_publish = now
        self._snapshot_pub.publish(String(data=json.dumps(snapshot)))
        if not consume:
            return
        with self._lock:
            self._learned.clear()  # the "learned:" line is shown once (decision 7)

    def _publish_events(self, tracks: Sequence[TrackState], now: float, enrolled: Mapping[str, bytes | None]) -> None:
        learned, hints = self._lines(now)
        events = self._events.emit(tracks, now, enrolled=enrolled, learned=learned, hints=hints)
        with self._lock:
            recalls = list(self._pending_recalls)
            self._pending_recalls.clear()
        for recall in recalls:
            event = self._events.recalled(recall.person_id, recall.name, recall.text, recall.stamp)
            if event is not None:
                events.append(event)
        for event in events:
            self._events_pub.publish(String(data=json.dumps(event)))

    def _lines(self, now: float) -> tuple[dict[str, str], dict[str, str]]:
        with self._lock:
            self._hints = {tag: line for tag, line in self._hints.items() if now - line[1] <= HINT_TTL_SEC}
            return dict(self._learned), {tag: text for tag, (text, _) in self._hints.items()}

    def _health(self, now: float) -> PeopleHealthDict:
        health = cast("PeopleHealthDict", dict(self._engine.health(now)))
        health["scribe"] = str(self._scribe_health())
        return health

    def _scribe_health(self) -> HealthState:
        if not self._config.scribe:
            return HealthState.NONE
        if self._scribe is None:
            return HealthState.UNAVAILABLE
        return HealthState.STALE if self._scribe.queued else HealthState.OK

    # -------------------------------------------------------- scribe thread

    def _scribe_loop(self) -> None:
        last_tick = 0.0
        while not self._stop.is_set():
            try:
                last_tick = self._scribe_cycle(last_tick)
            except Exception as error:  # noqa: BLE001 — one bad window must not end the scribe
                self._logger.error(f"[People] scribe cycle failed: {error!r}", throttle_duration_sec=10.0)
                self._stop.wait(2.0)

    def _scribe_cycle(self, last_tick: float) -> float:
        scribe = self._scribe
        if scribe is None:
            self._stop.wait(1.0)
            return last_tick
        self._run_forgets(scribe)
        try:
            message = self._chat.get(timeout=0.5)
        except queue.Empty:
            message = None
        if message is not None:
            self._apply_changes(scribe.observe(message, time.time()))
        now = time.time()
        if now - last_tick >= SCRIBE_TICK_SEC:
            last_tick = now
            self._apply_changes(scribe.tick(now))
        self._run_recalls(scribe)
        self._run_descriptions()
        return last_tick

    def _apply_changes(self, changes: Sequence[Change]) -> None:
        for change in changes:
            if change.kind is ChangeKind.NAME:
                with self._lock:
                    self._learned[change.tag] = change.text
                    self._hints.pop(change.tag, None)
            elif change.kind is ChangeKind.NAME_CANDIDATE:
                with self._lock:
                    self._hints[change.tag] = (change.text, time.time())

    def _run_forgets(self, scribe: Scribe) -> None:
        while True:
            try:
                person_id = self._forgets.get_nowait()
            except queue.Empty:
                return
            scribe.forget(person_id)

    def _run_recalls(self, scribe: Scribe) -> None:
        while True:
            try:
                person_id, question = self._recalls.get_nowait()
            except queue.Empty:
                return
            answer = scribe.recall(person_id, question)
            recall = Recall(person_id, self._store.name_of(person_id), answer, time.time())
            with self._lock:
                self._pending_recalls.append(recall)

    def _run_descriptions(self) -> None:
        if self._transport is None:
            return
        while True:
            try:
                person_id, crop = self._descriptions.get_nowait()
            except queue.Empty:
                return
            text = description.describe(self._transport, crop, model=self._config.gemini_model)
            if text:
                self._store.set_description(person_id, text, now=time.time())


def _payload(data: str) -> dict | None:
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _offer(destination: queue.Queue[Any], item: Any) -> None:
    """Never block a ROS callback or the engine thread on a full queue: what
    waits behind these is a Gemini call, and dropping one costs less than
    stalling the executor."""
    try:
        destination.put_nowait(item)
    except queue.Full:
        return
