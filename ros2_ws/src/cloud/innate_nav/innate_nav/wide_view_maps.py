#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pinhole -> pinhole resampling, the geometry behind wide_view.

Two cameras sharing an optical axis differ only in focal length and principal
point, so the map is affine: a pixel at angle theta off-axis sits at
f*tan(theta) from the centre in both, and the ratio of focal lengths is the
scale. Widening the field of view is not possible here -- a ray the source
never captured cannot be resampled into existence, which is what `valid`
reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Pinhole:
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int

    @classmethod
    def from_hfov(cls, hfov_deg: float, width: int, height: int) -> Pinhole:
        fx = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
        return cls(fx, fx, width / 2.0, height / 2.0, width, height)

    @property
    def k(self) -> list[float]:
        """Row-major 3x3 for sensor_msgs/CameraInfo."""
        return [self.fx, 0.0, self.cx, 0.0, self.fy, self.cy, 0.0, 0.0, 1.0]

    def half_angles_deg(self) -> tuple[float, float]:
        """Horizontal and vertical half-angles actually spanned, taking the
        principal point's offset into account -- a camera whose centre sits low
        reaches further up than down."""
        h = math.degrees(math.atan(min(self.cx, self.width - self.cx) / self.fx))
        v = math.degrees(math.atan(min(self.cy, self.height - self.cy) / self.fy))
        return h, v


def resample_maps(src: Pinhole, dst: Pinhole) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(map_x, map_y, valid) for cv2.remap, each (dst.height, dst.width).

    Pixels the source does not cover are pointed off-image so remap's border
    handling blanks them, rather than sampling an arbitrary edge texel and
    inventing content.
    """
    us, vs = np.meshgrid(np.arange(dst.width), np.arange(dst.height))
    x = src.cx + (us - dst.cx) * (src.fx / dst.fx)
    y = src.cy + (vs - dst.cy) * (src.fy / dst.fy)
    # Tolerance, not sloppiness: a ray landing exactly on pixel 0 comes back as
    # -1e-14 in floating point, and a bare >= 0 would blank the border column.
    e = 1e-6
    inside = (x >= -e) & (x <= src.width - 1 + e) & (y >= -e) & (y <= src.height - 1 + e)
    map_x = np.where(inside, x, -1.0).astype(np.float32)
    map_y = np.where(inside, y, -1.0).astype(np.float32)
    return map_x, map_y, inside


def coverage(src: Pinhole, dst: Pinhole) -> float:
    """Fraction of the requested view the source actually reaches."""
    return float(resample_maps(src, dst)[2].mean())
