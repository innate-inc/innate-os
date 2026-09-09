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
overlay by. Nothing here decides anything: the decisions are pure and live in
``node_config``, ``camera_feed``, ``publishing``, ``mutations``, ``transcript``
and ``recall``."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from brain_messages.srv import ForgetPerson, GetPeople, MergePeople, RenamePerson, SetPeopleCollection
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from std_msgs.msg import String

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.people import description, native_frames
from brain_client.people.camera_feed import (
    CameraFrame,
    decode_frame,
    decode_period,
    engine_active,
    motion_jpeg,
    native_deadline,
    pair_native,
    stamp_ns,
    stamp_text,
    wants_native,
)
from brain_client.people.geometry import CameraModel
from brain_client.people.mutations import (
    STALE_DECISION_MESSAGE,
    MutationLog,
    MutationResult,
    resolve_who,
    tag_newer_than_decision,
)
from brain_client.people.node_config import COMPRESSED_IMAGE_TOPIC, TickSource
from brain_client.people.publishing import (
    apply_conflicts,
    empty_snapshot,
    fresh_tracks,
    publishable_tracks,
    roster_answer,
    seek_hint,
    should_publish,
)
from brain_client.people.quality import EgoMotionTracker
from brain_client.people.recall import Recall, alone_in_view, is_memory_question, mentions_memory, recall_person
from brain_client.people.scribe_rules import ChangeKind
from brain_client.people.surfacing import PeopleEvents, build_snapshot, choose_attention
from brain_client.people.transcript import (
    SPEAKING_HOLD_SEC,
    chat_in_utterance,
    chat_out_utterance,
    held_speaking,
    speaking_tag,
    tag_views,
)
from brain_client.people.types import HealthDict, HealthState
from brain_client.perception.motion_gate import MotionGate

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import numpy as np
    from rclpy.node import Node
    from rclpy.publisher import Publisher
    from rclpy.subscription import Subscription

    from brain_client.people.engine import EngineConfig, PeopleEngine
    from brain_client.people.node_config import PeopleNodeConfig
    from brain_client.people.quality import EgoMotion
    from brain_client.people.resolve import Resolution
    from brain_client.people.scribe import Scribe, Transport
    from brain_client.people.scribe_rules import Change
    from brain_client.people.store import PeopleStore
    from brain_client.people.transcript import TagView, Utterance
    from brain_client.people.types import PeopleSnapshotDict, Pose, TrackState

SNAPSHOT_TOPIC = "/brain/people"
EVENTS_TOPIC = "/brain/people_events"
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

HINT_TTL_SEC = 120.0  # a disambiguation question ages out with the exchange it came from
LEARNED_TTL_SEC = 120.0  # and so does a "P5 = Zoe" line whose track left before it could be shown
ENROLLING_SEC = 60.0  # how long after enrolment a track still counts as "enrolling" to the scribe
IDLE_POLL_SEC = 0.5
TICK_POLL_SEC = 0.05
SCRIBE_TICK_SEC = 2.0
STORE_TICK_SEC = 60.0
EXPIRE_EVERY_SEC = 3600.0

ScribeLines = tuple[dict[str, str], dict[str, str]]
"""The scribe's per-tag ``learned`` and ``hint`` lines, read once per tick."""

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
        self._motion_sampled = 0.0
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

    def _on_compressed_image(self, msg: CompressedImage) -> None:
        if not msg.data:
            return
        frame = CameraFrame(stamp_ns(msg.header.stamp.sec, msg.header.stamp.nanosec), bytes(msg.data))
        self._observe(frame)

    def _on_raw_image(self, msg: Image) -> None:
        if not msg.data:
            return
        self._observe(
            CameraFrame(
                stamp_ns(msg.header.stamp.sec, msg.header.stamp.nanosec),
                bytes(msg.data),
                encoding=msg.encoding or "bgr8",
                width=msg.width,
                height=msg.height,
            )
        )

    def _observe(self, frame: CameraFrame) -> None:
        """The newest frame for the engine, and the same frame through the duty
        cycle's motion gate — on either tick source, or ``motion`` is False for
        the life of a node started on the raw one."""
        with self._lock:
            self._frame = frame
            ego = self._ego.state(time.time())
        # Outside the lock: the gate decodes, and the engine thread must never
        # wait on a decode to read the frame it is about to work on.
        jpeg = motion_jpeg(frame, self._motion_sampled)
        if jpeg is None:
            return
        self._motion_sampled = time.monotonic()
        if self._motion.observe(jpeg, ego.head_pitch_deg, ego.recently_driven):
            self._motion_until = time.time() + self._motion_burst_sec

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
        self._learned: dict[str, tuple[str, float]] = {}
        self._hints: dict[str, tuple[str, float]] = {}
        self._enrolling: dict[str, float] = {}
        self._speaking: tuple[str, float] | None = None
        self._forgotten: set[str] = set()
        self._pending_recalls: deque[Recall] = deque(maxlen=32)
        self._want_native = False
        self._native_until = 0.0
        self._uid = 0
        self._expired_at = time.monotonic()
        self._last_frame_at = 0.0

        self._chat: queue.Queue[Utterance] = queue.Queue(maxsize=64)
        # Unbounded, all three: a mutation the services already answered "done"
        # is not one the engine thread may drop (RFC section 10).
        self._forgets: queue.Queue[str] = queue.Queue()
        self._suppressions: queue.Queue[str] = queue.Queue()
        self._rebinds: queue.Queue[tuple[str, str]] = queue.Queue()
        self._recalls: queue.Queue[tuple[str, str, bool]] = queue.Queue(maxsize=8)
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
        if self._config.tick_source == TickSource.RAW:
            self._logger.warning(
                f"[People] tick_source=raw: the brain's frame ring holds only {COMPRESSED_IMAGE_TOPIC}, "
                "so it cannot pair these stamps and the overlay draws no boxes"
            )
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

    def _activation_tick(self) -> None:
        active = engine_active(always_on=self._config.always_on, brain_active=self._sensors.brain_active)
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
        answer = roster_answer(snapshot, roster=roster, capacity_full=self._store.capacity_full())
        # The snapshot is as old as the last tick, and Settings reads this the
        # instant it flips the switch.
        answer["collection_enabled"] = self._store.collection_enabled()
        response.success = True
        response.message = ""
        response.json = json.dumps(answer)
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
            self._rebinds.put((source, target))
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
            with self._lock:
                self._forgotten.add(person_id)
            self._suppress(person_id)
            if self._scribe is not None:
                # The queue and the open window are the scribe thread's; handing
                # it the id keeps this off the executor and off its file (RFC 10).
                self._forgets.put(person_id)
        return MutationResult(success, message)

    def _mutate(self, idempotency_key: str, write: Callable[[], MutationResult]) -> MutationResult:
        answered = self._mutations.answered(idempotency_key)
        if answered is not None:
            return answered
        result = write()
        if result.success:
            # A failure wrote nothing, so its retry is a fresh mutation — and a
            # memoized full disk would answer the retry the key exists for.
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

    def _revoked(self, person_id: str) -> bool:
        """Whether a forget has taken back the work still in flight for this
        person (RFC section 10). The store's tombstone is the durable answer;
        the set is this node's own copy, read without the store lock every tick
        contends on."""
        with self._lock:
            if person_id in self._forgotten:
                return True
        return self._store.is_tombstoned(person_id)

    def _suppress(self, person_id: str) -> None:
        """A forgotten person must stop being that person on the live track, or
        the next tick names an id that no longer exists. The resolver is the
        engine thread's, so the tag crosses to it and :meth:`_tick` applies it."""
        for track in self.tracks():
            if track.identity.person_id == person_id:
                self._suppressions.put(track.tag)

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
        self._feed_scribe(utterance)
        # Recall reads the record rather than adding to it, so it outlives the
        # collection switch.
        self._maybe_recall(utterance.text, tracks)

    def _on_chat_out(self, msg: String) -> None:
        payload = _payload(msg.data)
        if payload is None:
            return
        now = time.time()
        views = self._views(self.tracks(), now)
        self._feed_scribe(chat_out_utterance(payload, uid=self._next_uid(), now=now, in_view=views))

    def _feed_scribe(self, utterance: Utterance | None) -> None:
        """One transcript line for the scribe, unless the owner switched
        collection off: buffering a conversation is collecting it (RFC 10)."""
        if utterance is not None and self._store.collection_enabled():
            _offer(self._chat, utterance)

    def _maybe_recall(self, text: str, tracks: Sequence[TrackState]) -> None:
        # The roster is built under the store lock every tick contends on, and
        # almost no chat line is a memory question.
        if self._scribe is None or not mentions_memory(text):
            return
        roster = self._store.roster()
        subject = is_memory_question(text, [entry.get("name") or "" for entry in roster])
        if subject is None:
            return
        person_id = recall_person(subject, tracks, roster)
        if person_id is not None:
            _offer(self._recalls, (person_id, text, alone_in_view(person_id, tracks)))

    def _next_uid(self) -> str:
        with self._lock:
            self._uid += 1
            return f"u_{self._uid}"

    def _views(self, tracks: Sequence[TrackState], now: float) -> tuple[TagView, ...]:
        with self._lock:
            enrolling = [tag for tag, at in self._enrolling.items() if now - at <= ENROLLING_SEC]
        return tag_views(tracks, enrolling)

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
        active = engine_active(always_on=self._config.always_on, brain_active=self._sensors.brain_active)
        if not active:
            self._want_native = False
            self._native_until = 0.0
            self._publish_snapshot(now, (), None, (640, 480), self._lines(now), consume=False)
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
        started = time.monotonic()
        try:
            self._measure(now, ego, motion)
        finally:
            # Stamped when the work finished, not when it started: a tick slower
            # than its own period would otherwise fall due the instant it
            # returned, running the engine back to back on a saturated Jetson.
            self._last_tick = now + (time.monotonic() - started)

    def _measure(self, now: float, ego: EgoMotion, motion: bool) -> None:
        """One frame through the engine, published. Called only when the duty
        cycle says a tick is due."""
        frame = self._sensors.take_frame()
        image = decode_frame(frame) if frame is not None else None
        if frame is None or image is None:
            if frame is not None:
                self._logger.warn("[People] undecodable camera frame", throttle_duration_sec=10.0)
            self._publish_snapshot(now, self.tracks(), None, (640, 480), self._lines(now), consume=False)
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
        # Before the tags reach anyone: a native-library crash and a respawn two
        # seconds later must not hand P<n> to a second person (a no-op unless
        # this tick minted one).
        self._store.set_next_tag(self._engine.tracker.next_tag)
        self._native_until = native_deadline(
            wants_native(tracks, now, refresh_sec=self._engine_config.face_refresh_sec), now, self._native_until
        )
        self._want_native = now < self._native_until
        resolutions = self._engine.resolutions()
        resolved = apply_conflicts(tracks, resolutions)
        with self._lock:
            self._tracks = tuple(resolved)
        # Read once: a name the scribe commits between the events and the
        # snapshot would be consumed by the snapshot with no event to announce it.
        lines = self._lines(now)
        self._after_tick(image, resolved, resolutions, now, lines)
        # The engine's stamp, not this frame's: a tick its own duty cycle skipped
        # measured nothing, and the brain draws only on the frame it measured.
        self._publish_snapshot(
            now, resolved, self._engine.frame_stamp_ns, (image.shape[1], image.shape[0]), lines, consume=True
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
        self,
        image: np.ndarray,
        tracks: Sequence[TrackState],
        resolutions: Mapping[str, Resolution],
        now: float,
        lines: ScribeLines,
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
        self._publish_events(tracks, now, enrolled, lines)

    def _queue_description(self, person_id: str, image: np.ndarray, tracks: Sequence[TrackState], tag: str) -> None:
        if self._scribe is None or not self._store.collection_enabled() or self._store.description(person_id):
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
        lines: ScribeLines,
        *,
        consume: bool,
    ) -> None:
        """``consume`` clears the scribe's "learned:" lines, and only the tick
        that published their events may do it: a tick with no frame publishes no
        events, so clearing there loses the name the brain never heard about."""
        visible = publishable_tracks(tracks, now)
        learned, hints = lines
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
        shown = {tag for person in snapshot.get("people", ()) if person.get("learned") and (tag := person.get("tag"))}
        with self._lock:
            for tag in shown:  # shown once (decision 7) — and only what was shown
                self._learned.pop(tag, None)

    def _publish_events(
        self, tracks: Sequence[TrackState], now: float, enrolled: Mapping[str, bytes | None], lines: ScribeLines
    ) -> None:
        learned, hints = lines
        events = self._events.emit(tracks, now, enrolled=enrolled, learned=learned, hints=hints)
        with self._lock:
            recalls = list(self._pending_recalls)
            self._pending_recalls.clear()
        for recall in recalls:
            if self._revoked(recall.person_id):
                continue  # the last gate before a forgotten person's memories go on the wire
            event = self._events.recalled(recall.person_id, recall.name, recall.text, recall.stamp)
            if event is not None:
                events.append(event)
        for event in events:
            self._events_pub.publish(String(data=json.dumps(event)))

    def _lines(self, now: float) -> ScribeLines:
        """The scribe's lines, and the tick's sweep of everything keyed by a
        tag: a tag is never reused, so an entry whose track left would otherwise
        wait for a tick that can never come."""
        with self._lock:
            self._hints = {tag: line for tag, line in self._hints.items() if now - line[1] <= HINT_TTL_SEC}
            self._learned = {tag: line for tag, line in self._learned.items() if now - line[1] <= LEARNED_TTL_SEC}
            self._enrolling = {tag: at for tag, at in self._enrolling.items() if now - at <= ENROLLING_SEC}
            return (
                {tag: text for tag, (text, _) in self._learned.items()},
                {tag: text for tag, (text, _) in self._hints.items()},
            )

    def _health(self, now: float) -> PeopleHealthDict:
        health = cast("PeopleHealthDict", dict(self._engine.health(now, native_wanted=self._want_native)))
        health["scribe"] = str(self._scribe_health())
        return health

    def _scribe_health(self) -> HealthState:
        if not self._config.scribe:
            return HealthState.NONE
        if self._scribe is None:
            return HealthState.UNAVAILABLE
        return HealthState.STALE if self._scribe.queued else HealthState.OK

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
                    self._learned[change.tag] = (change.text, time.time())
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
                person_id, question, alone = self._recalls.get_nowait()
            except queue.Empty:
                return
            # Nothing about a forgotten person: not the Gemini call, and not the
            # event either when the forget lands while the call is in flight.
            if self._revoked(person_id):
                continue
            answer = scribe.recall(person_id, question, alone_in_view=alone)
            if self._revoked(person_id):
                continue
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
            # Both may have changed since the crop was queued, and this is the
            # last point before it leaves the robot.
            if not self._store.collection_enabled() or self._revoked(person_id):
                continue
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
    """Never block a ROS callback on a full queue. Only the bounded queues reach
    here — a transcript line, a recall question, a crop for a description — and
    what waits behind all three is a Gemini call, so dropping one costs less
    than stalling the executor. A mutation already answered "done" is never
    offered: those queues are unbounded."""
    try:
        destination.put_nowait(item)
    except queue.Full:
        return
