# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Builds the spatial memory while the robot drives around a mapped area.

Always on, independent of brain activation: a webapp teleop tour must build
memories too, so the recorder owns its own lightweight subscriptions instead
of borrowing the brain's activation-gated sensors. One rule governs capture —
record only what a well-localized robot saw. In navigation that means AMCL's
covariance holding below the webapp's "confident" thresholds for a few seconds
straight; in mapping there is no AMCL, so the SLAM estimate is taken at its
word while a fresh grid proves slam_toolbox is alive, and the memories stage
under the store's mapping session until ``/nav/map_saved`` hands them to the
saved map. Then the admission policy (memory/selection.py) decides novelty,
refresh, and eviction.
Novelty is visibility paint: each memory paints the floor its camera saw, and
a new picture is taken while the wedge ahead is mostly unpainted. A kept
viewpoint's picture refreshes only from a frame aimed the same way with
a clear sight line between the capture points, so an oblique, mid-turn, or
behind-a-wall frame never overwrites a straight-on view. Every stored
frame must first pass the quality gates (memory/quality.py); a bad frame never
clobbers a good view.

Each tick records the SHARPEST keepable frame seen since the last one, paired
with the pose at that frame's capture moment — not whatever the feed holds at
tick time. During a quick turn the blurred sweep loses to the crisp instant at
a pause, and that instant keeps its own heading even though the tick is 1 Hz.

Also mirrors the memory positions on the latched ``/brain/memory_positions``
(the topic the webapp and mobile map overlays watch) — published only when the
payload changes, so an idle robot costs one replayed message, not 6 KB/s —
and nudges the search's context cache to re-warm once recording quiets down.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import OccupancyGrid
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.memory.coverage import Coverage
from brain_client.memory.quality import MIN_SHARPNESS, frame_sharpness
from brain_client.memory.selection import plan_admission
from brain_client.memory.store import StaleStageError
from brain_client.state.map import Map

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclpy.node import Node
    from rclpy.publisher import Publisher

    from brain_client.core.config import BrainConfig
    from brain_client.memory.store import MemoryStore
    from brain_client.perception.pose_tracking import PoseTracker

_TICK_SEC = 1.0
_POSITION_VAR_MAX = 0.10  # m² — the webapp's "confident" localization threshold
_YAW_VAR_MAX = 0.18  # rad² — from the cloud-era pose graph gate
_CONFIDENT_FOR_SEC = 3.0  # covariance must hold below the thresholds this long before recording
_FRESH_FRAME_SEC = 2.5  # the compressed feed runs ~7.5 Hz; older means it died
# Our AMCL runs update_min_* = 0 (amcl.yaml): it publishes every scan while
# alive. Older means AMCL died — TF keeps composing odom motion while the last
# good covariance stays latched, and those poses are vouched for by nothing.
_COVARIANCE_FRESH_SEC = 5.0
# Mapping's analogue: slam_toolbox republishes /map every 0.5 s
# (map_update_interval). An older grid means SLAM died, and TF's map frame is
# coasting on odom alone.
_GRID_FRESH_SEC = 5.0
# /nav/map_saved is latched so a recorder respawning across the save still
# promotes the tour; the same latch replays that save to every later restart,
# and an old one must not adopt a newer stage into its foreign frame.
_SAVE_ANNOUNCEMENT_FRESH_SEC = 30.0
_MIN_HEAD_PITCH_DEG = -25.0  # looking further down films the floor, not the room
_MAPPING_MODES = frozenset({"mapping", "autonomous_mapping"})


@dataclass(frozen=True)
class _Candidate:
    """The sharpest keepable frame of the current tick window, with the pose
    it was captured from."""

    arrived: float  # monotonic
    x: float
    y: float
    theta: float
    sharpness: float
    jpeg: bytes


class MemoryRecorder:
    def __init__(
        self,
        node: Node,
        config: BrainConfig,
        *,
        store: MemoryStore,
        pose_tracker: PoseTracker,
        warm_search: Callable[[], None] | None,
        cache_state: Callable[[], str] | None,
        positions_pub: Publisher,
    ):
        self._logger = node.get_logger()
        self._store = store
        self._pose = pose_tracker
        self._warm_search = warm_search
        self._cache_state = cache_state
        self._positions_pub = positions_pub

        self._candidate: _Candidate | None = None
        self._last_published: dict | None = None
        self._nav_mode: str | None = None
        self._map_name = ""
        self._mapping_started: float | None = None  # the live SLAM session's identity (/nav/mapping_session)
        self._covariance: tuple[float, float, float] | None = None  # (var x, var y, var yaw)
        self._covariance_at = 0.0  # monotonic arrival of the last AMCL message
        self._head_pitch = 0.0
        self._confident_since: float | None = None
        self._grid: Map | None = None  # sight-line + paint truth for admissions and refreshes
        self._grid_at = 0.0  # monotonic arrival of the last /map message
        self._coverage = Coverage()

        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, depth=1
        )
        # The forked AMCL latches its pose (and map_server its grid); matching
        # TRANSIENT_LOCAL hands a late-joining recorder the last message
        # instead of silence.
        latched_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        node.create_subscription(CompressedImage, config.image_topic, self._on_image, image_qos)
        node.create_subscription(String, config.current_nav_mode_topic, self._on_nav_mode, 10)
        node.create_subscription(String, config.current_map_topic, self._on_current_map, 10)
        node.create_subscription(PoseWithCovarianceStamped, config.amcl_pose_topic, self._on_amcl_pose, latched_qos)
        node.create_subscription(String, config.map_saved_topic, self._on_map_saved, latched_qos)
        node.create_subscription(String, config.mapping_session_topic, self._on_mapping_session, latched_qos)
        node.create_subscription(OccupancyGrid, "/map", self._on_map, latched_qos)
        node.create_subscription(String, "/mars/head/current_position", self._on_head, 10)
        node.create_timer(_TICK_SEC, self.tick)

    # --- callbacks ---
    def _on_image(self, msg: CompressedImage) -> None:
        """Keep the sharpest keepable frame of the tick window. Scoring costs a
        JPEG decode per frame (~7.5 Hz), so it runs only while a record could
        actually happen."""
        if not msg.data or not self._recordable_moment():
            return
        pose = self._pose.map_pose_xyt()
        if pose is None:
            return
        jpeg = bytes(msg.data)
        sharpness = frame_sharpness(jpeg)
        if sharpness is None or sharpness < MIN_SHARPNESS:
            return
        best = self._candidate
        if best is None or sharpness > best.sharpness:
            self._candidate = _Candidate(time.monotonic(), *pose, sharpness, jpeg)

    def _recordable_moment(self) -> bool:
        if self._nav_mode in _MAPPING_MODES:
            return self._mapping_started is not None and self._slam_alive()
        return self._nav_mode == "navigation" and bool(self._map_name) and self._confident()

    def _on_nav_mode(self, msg: String) -> None:
        if msg.data != self._nav_mode:
            # A mode change swaps the coordinate frame; confidence held in the
            # old frame says nothing about the new one — the hold restarts,
            # and the latched /map replay is the old frame's too: only grids
            # received in this mode may vouch for SLAM (_slam_alive).
            self._confident_since = None
            self._grid = None
            self._grid_at = 0.0
        self._nav_mode = msg.data

    def _on_current_map(self, msg: String) -> None:
        self._map_name = msg.data

    def _on_mapping_session(self, msg: String) -> None:
        try:
            self._mapping_started = float(json.loads(msg.data)["started"])
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            self._logger.error(f"[Memory] unreadable mapping-session announcement: {msg.data!r}")

    def _on_map_saved(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
            map_name, stamp = str(payload["map"]), float(payload["stamp"])
            mapping_started = float(payload["mapping_started"]) if "mapping_started" in payload else None
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            self._logger.error(f"[Memory] unreadable map-save announcement: {msg.data!r}")
            return
        age = time.time() - stamp
        if age > _SAVE_ANNOUNCEMENT_FRESH_SEC:
            # The latch replaying an old save — see _SAVE_ANNOUNCEMENT_FRESH_SEC.
            self._logger.info(f"[Memory] ignoring a stale save announcement for {map_name} ({age:.0f}s old)")
            return
        try:
            promoted = self._store.promote_mapping_session(map_name, mapping_started)
        except StaleStageError as error:
            self._logger.error(f"[Memory] stage not promoted to {map_name} — another session built it: {error}")
            return
        except Exception as error:  # noqa: BLE001 — a full disk must not take the brain node down
            self._logger.error(f"[Memory] promoting the mapping session to {map_name} failed: {error!r}")
            return
        if promoted is None:
            self._logger.error(f"[Memory] mapping-session memories not promoted: {map_name} has no readable map file")
        elif promoted:
            self._logger.info(f"[Memory] promoted {promoted} mapping-session memories to {map_name}")

    def _on_amcl_pose(self, msg: PoseWithCovarianceStamped) -> None:
        covariance = msg.pose.covariance
        self._covariance = (covariance[0], covariance[7], covariance[35])
        self._covariance_at = time.monotonic()

    def _on_map(self, msg: OccupancyGrid) -> None:
        self._grid_at = time.monotonic()
        self._grid = Map(
            resolution=msg.info.resolution,
            width=msg.info.width,
            height=msg.info.height,
            origin_x=msg.info.origin.position.x,
            origin_y=msg.info.origin.position.y,
            origin_theta=quaternion_to_yaw(msg.info.origin.orientation),
            raw_source=msg,
        )

    def _on_head(self, msg: String) -> None:
        try:
            self._head_pitch = float(json.loads(msg.data)["current_position"])
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            pass  # keep the last known pitch; one corrupt message must not flip the gate

    # --- the 1 Hz tick ---
    def tick(self) -> None:
        try:
            if self._nav_mode in _MAPPING_MODES:
                # Entering the stage needs the session's identity: before the
                # latched /nav/mapping_session replay arrives, touching it
                # could wipe a half-tour this very session staged.
                if self._mapping_started is not None:
                    self._store.use_mapping_session(self._mapping_started)
                else:
                    # Detach meanwhile: the previous map's marks must not keep
                    # publishing over the growing grid (nor ever, against a
                    # mars_nav too old to announce sessions).
                    self._store.switch_map(None)
            else:
                self._store.switch_map(self._map_name or None)
            self._maybe_record()
            self._publish_positions()
            if self._warm_search is not None:
                self._warm_search()
        except Exception as error:  # noqa: BLE001 — a full disk must not take the brain node down
            self._logger.error(f"[Memory] tick failed: {error!r}")

    def _maybe_record(self) -> None:
        candidate, self._candidate = self._candidate, None  # each tick starts a fresh window
        if not self._localized_long_enough() or candidate is None:
            return
        if time.monotonic() - candidate.arrived > _FRESH_FRAME_SEC or self._head_pitch < _MIN_HEAD_PITCH_DEG:
            return
        self._record(candidate.x, candidate.y, candidate.theta, candidate.jpeg)

    def _localized_long_enough(self) -> bool:
        if not self._recordable_moment():
            self._confident_since = None
            return False
        now = time.monotonic()
        if self._confident_since is None:
            self._confident_since = now
        return now - self._confident_since >= _CONFIDENT_FOR_SEC

    def _confident(self) -> bool:
        if self._covariance is None or time.monotonic() - self._covariance_at > _COVARIANCE_FRESH_SEC:
            return False
        var_x, var_y, var_yaw = self._covariance
        return max(var_x, var_y) <= _POSITION_VAR_MAX and var_yaw <= _YAW_VAR_MAX

    def _slam_alive(self) -> bool:
        return self._grid is not None and time.monotonic() - self._grid_at <= _GRID_FRESH_SEC

    def _record(self, x: float, y: float, theta: float, jpeg: bytes) -> None:
        plan = plan_admission(self._store.snapshot().memories, x, y, theta, time.time(), self._grid, self._coverage)
        if not plan.record:
            return
        if plan.replace is not None:
            self._store.replace(plan.replace, x, y, theta, time.time(), jpeg)
            self._logger.info(f"[Memory] refreshed viewpoint {plan.replace.id} at ({x:.2f}, {y:.2f})")
            return
        if plan.evict is not None:
            self._store.evict(plan.evict)
        memory = self._store.add(x, y, theta, time.time(), jpeg)
        if memory is not None:
            evicted = f", evicted {plan.evict.id}" if plan.evict is not None else ""
            self._logger.info(f"[Memory] recorded viewpoint {memory.id} at ({x:.2f}, {y:.2f}){evicted}")

    def _publish_positions(self) -> None:
        snapshot = self._store.snapshot()
        payload = {
            # The store's identity, not /nav/current_map: during mapping the
            # positions live in the SLAM frame under the session name, and mid
            # map-verification the store still holds the previous map.
            "map": snapshot.map_name or "",
            # The webapp keys its per-map state on map+fingerprint: a same-name
            # re-map wipes the store and restarts ids, and only the fingerprint
            # betrays that to a client watching the name.
            "fingerprint": snapshot.fingerprint[:12],
            "cache": self._cache_state() if self._cache_state is not None else "off",
            "positions": snapshot.positions(),
        }
        # Gate on the whole payload, not store.revision: `cache` flips by wall
        # clock (a warm() completing, a handle expiring) without a store change.
        if payload == self._last_published:
            return
        self._last_published = payload
        self._positions_pub.publish(String(data=json.dumps(payload)))
