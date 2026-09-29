# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Straighten a saved map so its walls run along the grid axes.

SLAM keeps whatever heading the robot booted in, so buildings land at an angle.
At save, map_saver's PGM is resampled into the SLAM frame turned about its
origin by the smallest rotation that squares the walls: a session pose
(x, y, yaw) lands in the saved map at R(rotation)·(x, y), yaw + rotation.
The origin yaw stays 0 — AMCL, the costmap static layer and grid_localizer
ignore it — so the turn lives in the pixels.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import yaml

# map_saver's trinary PGM values.
OCCUPIED = 0
UNKNOWN = 205

MIN_WALL_CELLS = 50
COARSE_STEP = math.radians(1.0)
FINE_STEP = COARSE_STEP / 10
# A long wall's peak is narrower than COARSE_STEP: sampled off-centre it can
# score below a lesser wall family that sits on a sample, so refine several.
COARSE_PEAKS = 4
# Without a clearly sharper alignment (one cluttered room, a sparse scan) the
# best angle is noise, and the map stays as recorded.
MIN_GAIN = 1.1
# Below this a resample costs more wall fidelity than it straightens.
MIN_TILT = math.radians(0.25)
# Bilinear share of a source wall that keeps an output cell a wall: low enough
# that a one-cell diagonal wall comes out unbroken.
WALL_WEIGHT = 0.25


def straighten_saved_map(yaml_path: Path) -> float:
    """Rewrite map_saver's output at ``yaml_path`` squared to its grid; returns
    the rotation applied, 0 when the map was left as recorded."""
    import cv2  # deferred: mode_manager imports this module at startup, OpenCV joins it only once a save needs it

    meta = yaml.safe_load(yaml_path.read_text())
    if not isinstance(meta, dict):
        raise ValueError(f"{yaml_path.name} is not a map yaml")
    image_path = yaml_path.parent / meta["image"]
    image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise OSError(f"unreadable map image {image_path}")
    rotation = -wall_angle(image)
    if abs(rotation) < MIN_TILT:
        return 0.0
    straight, origin = straighten(image, meta["resolution"], (meta["origin"][0], meta["origin"][1]), rotation)
    ok, pgm = cv2.imencode(".pgm", straight)
    if not ok:
        raise OSError(f"could not encode the straightened {image_path.name}")
    meta["origin"] = [origin[0], origin[1], 0.0]
    # Both staged before either lands: a full disk must not leave a new
    # image under the old origin.
    image_tmp = image_path.with_name(f"{image_path.name}.tmp")
    yaml_tmp = yaml_path.with_name(f"{yaml_path.name}.tmp")
    image_tmp.write_bytes(pgm.tobytes())
    with yaml_tmp.open("w") as stream:
        yaml.safe_dump(meta, stream, sort_keys=False, default_flow_style=None)
    os.replace(image_tmp, image_path)
    os.replace(yaml_tmp, yaml_path)
    return rotation


def wall_angle(image: np.ndarray) -> float:
    """The dominant wall direction of a map_saver PGM, radians CCW in the map
    frame, folded into (-π/4, π/4]: the angle at which the wall cells,
    projected on two perpendicular axes, pile into the sharpest peaks."""
    rows, cols = np.nonzero(image == OCCUPIED)
    if rows.size < MIN_WALL_CELLS:
        return 0.0
    xs = cols.astype(np.float64)
    ys = (image.shape[0] - 1 - rows).astype(np.float64)
    reach = math.ceil(math.hypot(*image.shape)) + 1

    def peakiness(c: float, s: float) -> float:
        p = xs * c + ys * s + reach
        low = np.floor(p).astype(np.intp)
        share = p - low
        bins = np.bincount(low, 1 - share, 2 * reach + 2) + np.bincount(low + 1, share, 2 * reach + 2)
        return float(bins @ bins)

    def sharpness(theta: float) -> float:
        c, s = math.cos(theta), math.sin(theta)
        return peakiness(c, s) + peakiness(-s, c)

    coarse = [k * COARSE_STEP for k in range(-44, 46)]
    scores = [sharpness(theta) for theta in coarse]
    peaks = [i for i, score in enumerate(scores) if scores[i - 1] <= score >= scores[(i + 1) % len(scores)]]
    seeds = sorted(peaks, key=scores.__getitem__)[-COARSE_PEAKS:]
    best = max((coarse[i] + k * FINE_STEP for i in seeds for k in range(-10, 11)), key=sharpness)
    if sharpness(best) < MIN_GAIN * sharpness(0.0):
        return 0.0
    quarter = math.pi / 2
    return best - quarter * round(best / quarter - 1e-9)


def straighten(
    image: np.ndarray, resolution: float, origin: tuple[float, float], rotation: float
) -> tuple[np.ndarray, tuple[float, float]]:
    """``image`` resampled into its frame turned by ``rotation`` about the
    origin and cropped to its known cells, with the new grid's origin."""
    import cv2  # deferred: see straighten_saved_map

    to_map = _pixel_to_frame(resolution, origin, image.shape[0])
    c, s = math.cos(rotation), math.sin(rotation)
    turn = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    rows, cols = np.nonzero(image != UNKNOWN)
    known = (turn @ to_map @ np.vstack([cols, rows, np.ones_like(cols)]))[:2]
    low = known.min(axis=1) - resolution / 2
    width, height = np.ceil((known.max(axis=1) + resolution / 2 - low) / resolution - 1e-6).astype(int)
    straight_origin = (float(low[0]), float(low[1]))
    # straight pixel → turned frame → map frame (turnᵀ undoes the turn) → source pixel
    to_source = (np.linalg.inv(to_map) @ turn.T @ _pixel_to_frame(resolution, straight_origin, height))[:2]
    size = (int(width), int(height))
    straight = cv2.warpAffine(
        image, to_source, size, flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP, borderValue=(UNKNOWN,)
    )
    walls = cv2.warpAffine(
        (image == OCCUPIED).astype(np.float32), to_source, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP
    )
    straight[walls >= WALL_WEIGHT] = OCCUPIED
    return straight, straight_origin


def turn_pose(x: float, y: float, yaw: float, rotation: float) -> tuple[float, float, float]:
    """A SLAM-session pose in the saved map's frame, turned by ``rotation``."""
    c, s = math.cos(rotation), math.sin(rotation)
    return x * c - y * s, x * s + y * c, math.remainder(yaw + rotation, math.tau)


def _pixel_to_frame(resolution: float, origin: tuple[float, float], height: int) -> np.ndarray:
    """Pixel (col, row) centres → frame metres, as a 3×3 affine; PGM rows run top-down."""
    return np.array(
        [
            [resolution, 0.0, origin[0] + resolution / 2],
            [0.0, -resolution, origin[1] + (height - 0.5) * resolution],
            [0.0, 0.0, 1.0],
        ]
    )
