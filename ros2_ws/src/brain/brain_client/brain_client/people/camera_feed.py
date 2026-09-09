# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The node's side of the left-eye stream: one frame as it arrived, the pixels
it decodes into, the native buffer captured with it, and the duty cycle that
says how often any of that is worth doing (RFC 4.6). PURE module: cv2 and
numpy, no rclpy."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

from brain_client.people import native_frames
from brain_client.people.types import IdentityState
from brain_client.perception.motion_gate import MOTION_SAMPLE_SEC

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brain_client.people.engine import EngineConfig
    from brain_client.people.types import TrackState

NATIVE_SKEW_NS = 130_000_000
"""How far a native buffer's stamp may sit from the published frame's and still be
the same capture: two frames at 15 fps. The driver's own kMaxNativeSkewNs is a
wider PTS-reset guard, not this — a buffer further out than this is a different
moment, and cropping a face out of it would move the face."""
NATIVE_HOLD_SEC = 30.0  # how long the native subscription is held once anything wanted it
MOTION_JPEG_QUALITY = 50  # the gate diffs an eighth-scale gray image; anything finer is thrown away


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


def motion_jpeg(frame: CameraFrame, sampled_at: float, *, now: float | None = None) -> bytes | None:
    """The frame as the JPEG :class:`MotionGate` reads, or None when the gate
    would throw it away anyway. A raw frame has to be re-encoded to reach a gate
    written against the compressed topic, so it is only encoded at the gate's
    own sample interval — the quality is spent on an eighth-scale gray diff."""
    if frame.encoding == "jpeg":
        return frame.data
    if (now if now is not None else time.monotonic()) - sampled_at < MOTION_SAMPLE_SEC:
        return None
    image = decode_frame(frame)
    if image is None:
        return None
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), MOTION_JPEG_QUALITY])
    return bytes(buffer) if ok else None


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


def engine_active(*, always_on: bool, brain_active: bool) -> bool:
    """Whether the engine should be looking at all (RFC 3.1): the brain's
    lifecycle drives it unless the owner asked for "always on"."""
    return always_on or brain_active


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


def native_deadline(wanted: bool, now: float, deadline: float, *, hold_sec: float = NATIVE_HOLD_SEC) -> float:
    """Until when the native subscription is held. One settled person makes
    :func:`wants_native` alternate at the face-refresh interval, and following
    that literally would create and destroy a subscription every few seconds
    for as long as they stand there."""
    return max(deadline, now + hold_sec) if wanted else deadline
