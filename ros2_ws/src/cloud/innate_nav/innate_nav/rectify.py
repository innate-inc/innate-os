#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Fisheye -> pinhole remap, so the real camera can produce the view the policy
was trained on.

The policy only ever saw a 640x480 pinhole at 110 deg horizontal fov
(innate-nav-data's `mars` preset, front_rect). In sim that is rendered natively.
On the robot the sensor is a ~150 deg fisheye, so the same view has to be
resampled out of it: pick the virtual pinhole intrinsics, trace each output
pixel's ray, project that ray through the lens model, and sample there.

The mapping depends only on the two camera models, so it is built once and
reused; per frame the cost is a table lookup (cv2.remap), not this math.

Lens model is the double sphere of Usenko et al., which is what
innate-nav-data's `front_fish` preset uses (xi, alpha). A plumb_bob /
radial-tangential model is NOT a substitute at this field of view -- it is
fitted for mild distortion and diverges exactly in the outer region a 110 deg
crop depends on.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class DoubleSphere:
    """Source lens: intrinsics plus the double-sphere shape parameters."""

    fx: float
    fy: float
    cx: float
    cy: float
    xi: float
    alpha: float
    width: int
    height: int

    def project(self, rays: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Unit rays (N, 3) -> source pixels (u, v) and a validity mask.

        Points behind the lens's own horizon have no image; the mask marks
        them rather than letting them alias onto a valid-looking pixel.
        """
        r = np.asarray(rays, dtype=np.float64).reshape(-1, 3)
        x, y, z = r[:, 0], r[:, 1], r[:, 2]
        d1 = np.sqrt(x * x + y * y + z * z)
        zx = self.xi * d1 + z
        d2 = np.sqrt(x * x + y * y + zx * zx)
        denom = self.alpha * d2 + (1.0 - self.alpha) * zx

        a = self.alpha
        w1 = a / (1.0 - a) if a <= 0.5 else (1.0 - a) / a
        w2 = (w1 + self.xi) / np.sqrt(2.0 * w1 * self.xi + self.xi * self.xi + 1.0)
        valid = (z > -w2 * d1) & (np.abs(denom) > 1e-12)

        safe = np.where(valid, denom, 1.0)
        return self.fx * x / safe + self.cx, self.fy * y / safe + self.cy, valid

    def unproject(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        """Source pixels -> unit rays. Inverse of project(), for round-trips."""
        mx = (np.asarray(u, dtype=np.float64) - self.cx) / self.fx
        my = (np.asarray(v, dtype=np.float64) - self.cy) / self.fy
        r2 = mx * mx + my * my
        a = self.alpha
        mz = (1.0 - a * a * r2) / (a * np.sqrt(np.maximum(1.0 - (2.0 * a - 1.0) * r2, 0.0)) + 1.0 - a)
        k = (mz * self.xi + np.sqrt(np.maximum(mz * mz + (1.0 - self.xi * self.xi) * r2, 0.0))) / (mz * mz + r2)
        rays = np.stack([k * mx, k * my, k * mz - self.xi], axis=-1)
        return rays / np.linalg.norm(rays, axis=-1, keepdims=True)


@dataclass(frozen=True)
class Pinhole:
    """The virtual camera the policy expects."""

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int

    @classmethod
    def from_hfov(cls, hfov_deg: float, width: int, height: int) -> Pinhole:
        fx = (width / 2.0) / np.tan(np.radians(hfov_deg) / 2.0)
        return cls(fx, fx, width / 2.0, height / 2.0, width, height)

    def rays(self) -> np.ndarray:
        """Unit ray per output pixel, row-major."""
        us, vs = np.meshgrid(np.arange(self.width), np.arange(self.height))
        d = np.stack([(us - self.cx) / self.fx, (vs - self.cy) / self.fy, np.ones_like(us, float)], axis=-1)
        return (d / np.linalg.norm(d, axis=-1, keepdims=True)).reshape(-1, 3)

    @property
    def k(self) -> list[float]:
        """Row-major 3x3 for sensor_msgs/CameraInfo."""
        return [self.fx, 0.0, self.cx, 0.0, self.fy, self.cy, 0.0, 0.0, 1.0]


def build_maps(src: DoubleSphere, dst: Pinhole) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(map_x, map_y, valid) for cv2.remap, each (dst.height, dst.width).

    Invalid pixels are pointed off-image so remap's border handling blanks them
    instead of sampling an arbitrary edge texel.
    """
    u, v, ok = src.project(dst.rays())
    # Tolerance, not sloppiness: a ray that lands exactly on pixel 0 comes
    # back as -1e-14 after normalising, and a bare >= 0 would blank the border.
    e = 1e-6
    inside = ok & (u >= -e) & (u <= src.width - 1 + e) & (v >= -e) & (v <= src.height - 1 + e)
    shape = (dst.height, dst.width)
    map_x = np.where(inside, u, -1.0).reshape(shape).astype(np.float32)
    map_y = np.where(inside, v, -1.0).reshape(shape).astype(np.float32)
    return map_x, map_y, inside.reshape(shape)


def coverage(src: DoubleSphere, dst: Pinhole) -> float:
    """Fraction of the virtual view the lens actually reaches.

    Below 1.0 the requested fov exceeds the lens and the corners come back
    blank -- the check to run against a real calibration before trusting that
    a 150 deg diagonal lens covers 110 deg horizontally.
    """
    return float(build_maps(src, dst)[2].mean())
