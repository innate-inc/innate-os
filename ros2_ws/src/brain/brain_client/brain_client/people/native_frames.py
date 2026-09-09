# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The native MJPG path: the camera's own pixels, decoded only when needed.

The driver publishes the sensor's untouched 2560x720 MJPG buffer (both eyes,
side by side, *unrotated* — the published frames are the ones that are rotated
180 degrees). The published left eye is therefore the RIGHT half of that buffer
turned around:

    native_left = buffer[:, 1280:][::-1, ::-1]

Everything in this module exists so a face or a body embedding is computed from
1280x720 pixels rather than the 640x480 the rest of the stack sees — the
difference between a face system usable to 2.5 m and one usable to 1.3 m.
Decoding is not free (12-20 ms for the pair on the Orin), so the engine calls
in here only when a track actually needs evidence, and
:data:`cv2.IMREAD_REDUCED_COLOR_2` serves the cases where only a detector looks.

No ROS; cv2 and numpy only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from brain_client.people.geometry import PUBLISHED_HEIGHT, PUBLISHED_WIDTH

if TYPE_CHECKING:
    from brain_client.people.geometry import CameraModel
    from brain_client.people.types import Box

UNSQUASHED_SIZE = (PUBLISHED_WIDTH, round(PUBLISHED_HEIGHT * 0.75))  # 640x360: true proportions
FACE_CROP_MIN_PX = 160  # YuNet loses small faces below this; the crop is upscaled instead


def decode(jpeg: bytes, *, reduced: bool = False) -> np.ndarray | None:
    """Decode the native buffer; ``reduced`` halves it (a quarter of the work)
    for the passes where only a detector looks at the pixels."""
    if not jpeg:
        return None
    flag = cv2.IMREAD_REDUCED_COLOR_2 if reduced else cv2.IMREAD_COLOR
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), flag)


def left_eye(buffer: np.ndarray) -> np.ndarray:
    """The published left eye out of the side-by-side, unrotated buffer.

    Works on a reduced decode too: the split is taken at the buffer's own
    midpoint rather than a fixed 1280.
    """
    half = buffer.shape[1] // 2
    return np.ascontiguousarray(buffer[:, half:][::-1, ::-1])


def decode_left_eye(jpeg: bytes, *, reduced: bool = False) -> np.ndarray | None:
    buffer = decode(jpeg, reduced=reduced)
    return None if buffer is None else left_eye(buffer)


def unsquash_published(frame_bgr: np.ndarray) -> np.ndarray:
    """640x480 -> 640x360 so the detector sees a person's true proportions
    rather than the driver's 25% horizontal squash (RFC 4.1)."""
    if (frame_bgr.shape[1], frame_bgr.shape[0]) == UNSQUASHED_SIZE:
        return frame_bgr
    return cv2.resize(frame_bgr, UNSQUASHED_SIZE, interpolation=cv2.INTER_AREA)


def crop(image: np.ndarray, box: Box, *, margin: float = 0.0) -> np.ndarray | None:
    """A normalized box out of ``image``, grown by ``margin`` of its own size
    and clipped to the frame. None when nothing is left inside the frame."""
    height, width = image.shape[:2]
    ymin, xmin, ymax, xmax = box
    dy, dx = (ymax - ymin) * margin, (xmax - xmin) * margin
    x0 = max(0, int((xmin - dx) * width))
    y0 = max(0, int((ymin - dy) * height))
    x1 = min(width, int(round((xmax + dx) * width)))
    y1 = min(height, int(round((ymax + dy) * height)))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return image[y0:y1, x0:x1]


def crop_origin(image: np.ndarray, box: Box, *, margin: float = 0.0) -> tuple[int, int]:
    """Top-left pixel of the crop :func:`crop` would return — the offset the
    undistortion needs to shift the principal point by."""
    height, width = image.shape[:2]
    ymin, xmin, ymax, xmax = box
    dy, dx = (ymax - ymin) * margin, (xmax - xmin) * margin
    return (max(0, int((xmin - dx) * width)), max(0, int((ymin - dy) * height)))


def undistort_crop(crop_bgr: np.ndarray, camera: CameraModel, origin_px: tuple[int, int]) -> np.ndarray:
    """Undistort a crop in place of the whole frame, using the model's K with
    the principal point moved to the crop's own origin.

    A 116-degree lens is far from rectilinear at its edges, and a face high in
    the frame lives exactly there; an embedder trained on aligned faces sees
    that warp as a different person.
    """
    if not camera.distortion or crop_bgr.size == 0:
        return crop_bgr
    x0, y0 = origin_px
    k = np.array(
        [[camera.fx, 0.0, camera.cx - x0], [0.0, camera.fy, camera.cy - y0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    distortion = np.array(camera.distortion, dtype=np.float64).reshape(1, -1)
    return cv2.undistort(crop_bgr, k, distortion, None, k)


def upscale_for_detection(crop_bgr: np.ndarray, min_px: int = FACE_CROP_MIN_PX) -> np.ndarray:
    """Grow a small head region so the face detector has pixels to work with
    (RFC 4.2: x2 when the region is under 160 px)."""
    longest = max(crop_bgr.shape[0], crop_bgr.shape[1])
    if longest >= min_px or longest == 0:
        return crop_bgr
    scale = min(4.0, min_px / longest)
    size = (max(1, round(crop_bgr.shape[1] * scale)), max(1, round(crop_bgr.shape[0] * scale)))
    return cv2.resize(crop_bgr, size, interpolation=cv2.INTER_CUBIC)
