# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The people engine: one tick turns a camera frame into who is in the room —
detect on the un-squashed published frame, track, then only for tracks that
still need evidence and only while the robot holds still, crop head and body
out of the camera's native 1280x720 pixels, gate them and hand them to the
resolver — at the duty cycle of RFC 4.6 (docs/rfc/people-memory.md in
innate-jetson), which the engine enforces itself so the node may call
:meth:`tick` as fast as frames arrive. No ROS: the node hands in frames,
ego-motion and the map pose; cv2 and numpy only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

from brain_client.people import native_frames, quality
from brain_client.people.geometry import CameraModel, head_region, pose_from_landmarks
from brain_client.people.resolve import Resolver
from brain_client.people.surfacing import FACE_STALE_SEC
from brain_client.people.track import Tracker
from brain_client.people.types import (
    SETTLED_STATES,
    BodyObservation,
    FaceObservation,
    HealthDict,
    HealthState,
    TrackState,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from brain_client.people.backends import Backends
    from brain_client.people.resolve import Resolution
    from brain_client.people.track import Track
    from brain_client.people.types import Box, FaceHit, Identity, Pose, RosterView

_THUMBNAIL_PX = 160
_THUMBNAIL_QUALITY = 80


@dataclass(frozen=True)
class EngineConfig:
    """RFC 4.6 as numbers. Rates are ceilings: the engine never runs faster."""

    idle_detect_hz: float = 0.5
    active_detect_hz: float = 5.0
    driving_detect_hz: float = 2.0
    motion_burst_sec: float = 10.0
    native_unsettled_hz: float = 5.0
    native_settled_hz: float = 1.0
    face_refresh_sec: float = 5.0
    body_hz: float = 1.0
    height_hz: float = 1.0
    camera_stale_sec: float = 3.0
    native_stale_sec: float = 3.0
    face_crop_margin: float = 0.3
    body_crop_margin: float = 0.05
    range_relative_sigma: float = 0.05  # floor-ray range error grows with range


@dataclass
class _Runtime:
    """Per-track bookkeeping the engine owns; identity lives in the resolver."""

    frames_with_face: int = 0
    last_face_stamp: float | None = None
    last_face_embed: float = 0.0
    last_body_embed: float = 0.0
    last_height: float = 0.0
    range_m: float | None = None
    height_m: float | None = None
    bearing_deg: float | None = None
    head_box: Box | None = None
    reid_tried: bool = False


class PeopleEngine:
    """Detection, tracking, evidence and identity for one camera.

    Backends and the roster are injected: the node builds them, the tests fake
    them, and nothing in here knows whether a model is real.
    """

    def __init__(
        self,
        backends: Backends,
        roster: RosterView,
        *,
        camera: CameraModel | None = None,
        tracker: Tracker | None = None,
        resolver: Resolver | None = None,
        config: EngineConfig | None = None,
    ) -> None:
        self._backends = backends
        self._config = config or EngineConfig()
        self._camera = camera or CameraModel.published_default()
        self._tracker = tracker or Tracker()
        self._resolver = resolver or Resolver(roster)
        self._runtime: dict[str, _Runtime] = {}
        self._independence = quality.IndependenceGate()
        self._states: list[TrackState] = []
        self._resolutions: dict[str, Resolution] = {}
        self._last_detect = 0.0
        self._frame_stamp_ns: str | None = None
        self._last_native_decode = 0.0
        self._last_frame_stamp = 0.0
        self._last_native_stamp = 0.0
        self._motion_until = 0.0

    # ------------------------------------------------------------- accessors

    @property
    def tracker(self) -> Tracker:
        return self._tracker

    @property
    def resolver(self) -> Resolver:
        return self._resolver

    @property
    def camera(self) -> CameraModel:
        return self._camera

    def set_camera(self, camera: CameraModel) -> None:
        self._camera = camera

    def tracks(self) -> list[TrackState]:
        return list(self._states)

    @property
    def frame_stamp_ns(self) -> str | None:
        """The header stamp of the frame these boxes were measured on, which the
        brain pairs its overlay by (RFC section 7). It only moves on a tick that
        actually ran detection, so a tick the duty cycle skipped never restamps
        yesterday's boxes onto today's picture."""
        return self._frame_stamp_ns

    def resolutions(self) -> dict[str, Resolution]:
        """This tick's outcomes: enrolments, conflicts and switches the node
        turns into ``/brain/people_events`` entries."""
        return dict(self._resolutions)

    def health(self, now: float) -> HealthDict:
        return HealthDict(
            camera=str(self._freshness(self._last_frame_stamp, now, self._config.camera_stale_sec)),
            native=str(self._freshness(self._last_native_stamp, now, self._config.native_stale_sec)),
            face_model=self._backends.health.get("face_model", str(HealthState.UNAVAILABLE)),
            body_model=self._backends.health.get("body_model", str(HealthState.UNAVAILABLE)),
            gpu=self._backends.health.get("gpu", str(HealthState.NONE)),
        )

    @staticmethod
    def _freshness(last: float, now: float, stale_after: float) -> HealthState:
        if last <= 0.0:
            return HealthState.UNAVAILABLE
        return HealthState.OK if now - last <= stale_after else HealthState.STALE

    # ------------------------------------------------------------------ tick

    def tick(
        self,
        frame_bgr: np.ndarray | None,
        native_jpeg: bytes | None,
        now: float,
        ego: quality.EgoMotion,
        scan_legs: list[tuple[float, float]] | None = None,
        *,
        motion: bool = False,
        map_name: str | None = None,
        pose: Pose | None = None,
        speaking: Collection[str] = (),
        frame_stamp_ns: str | None = None,
    ) -> list[TrackState]:
        del scan_legs  # leg clustering is Phase 2; the signature is already its seat
        if native_jpeg:
            self._last_native_stamp = now
        if frame_bgr is None or frame_bgr.size == 0:
            return self._states
        self._last_frame_stamp = now
        if motion:
            self._motion_until = now + self._config.motion_burst_sec
        if now - self._last_detect < self._detect_period(now, ego):
            return self._states
        self._last_detect = now
        self._frame_stamp_ns = frame_stamp_ns

        frame = native_frames.unsquash_published(frame_bgr)
        detections = self._backends.detector.detect(frame)
        tracks = self._tracker.update(detections, now, ego)
        for tag in self._tracker.take_recovered():
            self._resolver.on_reassociated(tag, now)

        self._measure_geometry(tracks, now, ego.head_pitch_deg)
        if ego.still:
            self._gather_evidence(tracks, frame, native_jpeg, now)

        self._resolutions = self._resolver.resolve(self._tracker.all_tracks(), now, map_name=map_name, pose=pose)
        self._apply_splits()
        self._states = self._build_states(speaking, now)
        return self._states

    # ----------------------------------------------------------- duty cycle

    def _detect_period(self, now: float, ego: quality.EgoMotion) -> float:
        if ego.recently_driven:
            return 1.0 / self._config.driving_detect_hz
        if self._tracker.live() or now < self._motion_until:
            return 1.0 / self._config.active_detect_hz
        return 1.0 / self._config.idle_detect_hz

    def _native_period(self) -> float:
        unsettled = any(state.identity.state not in SETTLED_STATES for state in self._states)
        return 1.0 / (self._config.native_unsettled_hz if unsettled else self._config.native_settled_hz)

    def _wants_face(self, tag: str, identity: Identity, now: float) -> bool:
        runtime = self._runtime_of(tag)
        if identity.state not in SETTLED_STATES:
            return True  # unsettled: every passing crop counts
        return now - runtime.last_face_embed >= self._config.face_refresh_sec

    # ------------------------------------------------------------- evidence

    def _gather_evidence(self, tracks: list[Track], frame: np.ndarray, native_jpeg: bytes | None, now: float) -> None:
        wanted = {t.tag for t in tracks if self._wants_face(t.tag, self._resolver.identity(t.tag), now)}
        native = self._decode_native(native_jpeg, now) if wanted else None
        source = native if native is not None else frame
        native_camera = self._camera.native()
        for track in tracks:
            if track.tag in wanted:
                self._observe_face(track, source, native_camera, native is not None, now)
            self._observe_body(track, source, now)

    def _decode_native(self, native_jpeg: bytes | None, now: float) -> np.ndarray | None:
        if not native_jpeg or now - self._last_native_decode < self._native_period():
            return None
        decoded = native_frames.decode_left_eye(native_jpeg)
        if decoded is None:
            return None
        self._last_native_decode = now
        return decoded

    def _observe_face(
        self,
        track: Track,
        source: np.ndarray,
        native_camera: CameraModel,
        from_native: bool,
        now: float,
    ) -> None:
        locator, embedder = self._backends.locator, self._backends.face
        if locator is None:
            return
        region = head_region(track.box)
        crop = native_frames.crop(source, region, margin=self._config.face_crop_margin)
        if crop is None:
            return
        origin = native_frames.crop_origin(source, region, margin=self._config.face_crop_margin)
        if from_native:
            crop = native_frames.undistort_crop(crop, native_camera, origin)
        upscaled = native_frames.upscale_for_detection(crop)
        scale = upscaled.shape[0] / crop.shape[0] if crop.shape[0] else 1.0
        hits = locator.locate(upscaled)
        if not hits:
            return
        hit = max(hits, key=lambda h: h.h)
        box = _hit_box(hit, origin, scale, source.shape[1], source.shape[0])
        size_px = (box[2] - box[0]) * native_camera.height
        real_px = (box[2] - box[0]) * source.shape[0]
        if not quality.face_detectable(size_px):
            return

        runtime = self._runtime_of(track.tag)
        runtime.frames_with_face += 1
        runtime.last_face_stamp = now
        runtime.head_box = box
        track.head_box = box

        yaw, pitch = pose_from_landmarks(hit.landmarks)
        sharpness = quality.crop_sharpness(upscaled)
        luminance = quality.crop_luminance(upscaled)
        if not self._passes_face_gates(size_px, real_px, yaw, pitch, sharpness, luminance):
            return
        bucket = quality.pose_bucket(yaw, pitch)
        if not self._independence.accept(track.tag, now, box, bucket):
            return
        score = quality.quality_score(
            size_px=size_px, yaw_deg=yaw, pitch_deg=pitch, sharpness=sharpness, luminance=luminance
        )
        embedding = embedder.embed(upscaled, hit) if embedder is not None else None
        runtime.last_face_embed = now
        self._resolver.observe_face(
            track.tag,
            FaceObservation(
                stamp=now,
                box=box,
                size_px=size_px,
                real_px=real_px,
                yaw_deg=yaw,
                pitch_deg=pitch,
                sharpness=sharpness,
                luminance=luminance,
                quality=score,
                model=embedder.model if embedder is not None else "",
                embedding=embedding if embedding is not None and embedding.size else None,
                thumbnail=self._thumbnail(upscaled, size_px, real_px, yaw, pitch),
            ),
        )

    @staticmethod
    def _passes_face_gates(
        size_px: float, real_px: float, yaw: float, pitch: float, sharpness: float, luminance: float
    ) -> bool:
        return (
            quality.face_size_ok(size_px, real_px=real_px)
            and quality.face_pose_ok(yaw, pitch)
            and quality.face_sharp_enough(sharpness, size_px)
            and quality.luminance_ok(luminance)
        )

    def _observe_body(self, track: Track, source: np.ndarray, now: float) -> None:
        embedder = self._backends.body
        runtime = self._runtime_of(track.tag)
        if embedder is None or now - runtime.last_body_embed < 1.0 / self._config.body_hz:
            return
        crop = native_frames.crop(source, track.box, margin=self._config.body_crop_margin)
        if crop is None:
            return
        height_px = (track.box[2] - track.box[0]) * self._camera.native().height
        sharpness = quality.crop_sharpness(crop)
        runtime.last_body_embed = now
        if not (quality.body_size_ok(height_px) and quality.body_sharp_enough(sharpness)):
            return
        embedding = embedder.embed(crop)
        observation = BodyObservation(
            stamp=now,
            box=track.box,
            height_px=height_px,
            sharpness=sharpness,
            model=embedder.model,
            embedding=embedding if embedding.size else None,
        )
        self._resolver.observe_body(track.tag, observation, now)
        if embedding.size:
            self._reassociate(track, embedding, embedder.model, now)

    def _reassociate(self, track: Track, embedding: np.ndarray, model: str, now: float) -> None:
        """A track's first outfit embedding is its chance to turn out to be
        somebody who was here five minutes ago rather than a stranger."""
        runtime = self._runtime_of(track.tag)
        if runtime.reid_tried:
            self._tracker.note_body(track.tag, embedding, model)
            return
        runtime.reid_tried = True
        recovered = self._tracker.reassociate(track.tag, embedding, model, now)
        if recovered is None:
            return
        self._runtime[recovered] = self._runtime.pop(track.tag, _Runtime())
        self._runtime[recovered].reid_tried = True
        self._resolver.forget(track.tag)
        self._resolver.on_reassociated(recovered, now)

    def _measure_geometry(self, tracks: list[Track], now: float, head_pitch_deg: float) -> None:
        for track in tracks:
            runtime = self._runtime_of(track.tag)
            runtime.bearing_deg = self._camera.bearing_deg(track.box, head_pitch_deg)
            range_m = self._camera.floor_range(track.box, head_pitch_deg)
            runtime.range_m = range_m
            if range_m is None or track.box[0] <= 0.005:
                continue  # a head cut off by the frame top is not a height measurement
            height_m = self._camera.height_from_range(track.box, range_m, head_pitch_deg)
            runtime.height_m = height_m
            if height_m is None or now - runtime.last_height < 1.0 / self._config.height_hz:
                continue
            runtime.last_height = now
            self._resolver.observe_height(track.tag, height_m, (self._config.range_relative_sigma * range_m) ** 2)

    # --------------------------------------------------------------- outputs

    def _apply_splits(self) -> None:
        for tag, resolution in list(self._resolutions.items()):
            if not resolution.split_requested:
                continue
            new_tag = self._tracker.split(tag)
            if new_tag is None:
                continue
            self._resolver.apply_split(tag, new_tag)
            self._runtime.pop(tag, None)
            self._independence.forget(tag)

    def _build_states(self, speaking: Collection[str], now: float) -> list[TrackState]:
        states: list[TrackState] = []
        alive = {t.tag for t in self._tracker.all_tracks()}
        for tag in [t for t in self._runtime if t not in alive]:
            del self._runtime[tag]
            self._independence.forget(tag)
        for track in sorted(self._tracker.all_tracks(), key=lambda t: t.first_seen):
            runtime = self._runtime_of(track.tag)
            resolution = self._resolutions.get(track.tag)
            identity = resolution.identity if resolution is not None else self._resolver.identity(track.tag)
            states.append(
                TrackState(
                    tag=track.tag,
                    box=track.box,
                    head_box=self._head_box(track, runtime, now),
                    identity=identity,
                    first_seen=track.first_seen,
                    last_seen=track.last_seen,
                    lost=track.lost,
                    range_m=runtime.range_m,
                    bearing_deg=runtime.bearing_deg,
                    height_m=runtime.height_m,
                    speaking=track.tag in speaking,
                    frames_with_face=runtime.frames_with_face,
                    last_face_stamp=runtime.last_face_stamp,
                )
            )
        return states

    # ---------------------------------------------------------------- pieces

    @staticmethod
    def _head_box(track: Track, runtime: _Runtime, now: float) -> Box:
        """A face hit pins the head; detections carry none, so once the hit has
        gone stale the head follows the body again rather than staying where
        the person used to be standing."""
        stamp = runtime.last_face_stamp
        if runtime.head_box is not None and stamp is not None and now - stamp <= FACE_STALE_SEC:
            return runtime.head_box
        return head_region(track.box)

    def _runtime_of(self, tag: str) -> _Runtime:
        runtime = self._runtime.get(tag)
        if runtime is None:
            runtime = _Runtime()
            self._runtime[tag] = runtime
        return runtime

    @staticmethod
    def _thumbnail(crop_bgr: np.ndarray, size_px: float, real_px: float, yaw: float, pitch: float) -> bytes | None:
        """Only enrolment-grade crops are worth keeping; everything else would
        cost a JPEG encode per frame for a picture nobody stores."""
        if not quality.face_size_ok(size_px, quality.Purpose.ENROL, real_px=real_px):
            return None
        if not quality.face_pose_ok(yaw, pitch, quality.Purpose.ENROL):
            return None
        longest = max(crop_bgr.shape[0], crop_bgr.shape[1])
        image = crop_bgr
        if longest > _THUMBNAIL_PX:
            scale = _THUMBNAIL_PX / longest
            size = (max(1, round(crop_bgr.shape[1] * scale)), max(1, round(crop_bgr.shape[0] * scale)))
            image = cv2.resize(crop_bgr, size, interpolation=cv2.INTER_AREA)
        ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), _THUMBNAIL_QUALITY])
        return bytes(buffer) if ok else None


def _hit_box(hit: FaceHit, origin: tuple[int, int], scale: float, width: int, height: int) -> Box:
    """A locator hit, in upscaled-crop pixels, as a normalized box of the frame
    it was cropped from — which is the same box in the published frame, since
    normalized coordinates do not care which resolution they came from."""
    x0 = origin[0] + hit.x / scale
    y0 = origin[1] + hit.y / scale
    x1 = x0 + hit.w / scale
    y1 = y0 + hit.h / scale
    return (
        max(0.0, y0 / height),
        max(0.0, x0 / width),
        min(1.0, y1 / height),
        min(1.0, x1 / width),
    )
