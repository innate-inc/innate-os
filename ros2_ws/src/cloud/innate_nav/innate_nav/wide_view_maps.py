#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Geometry for wide_view: the policy's virtual camera, and the map that fills
it from the raw left image.

Undistortion here is monocular and independent of the stereo pipeline. That
pipeline rectifies with `alpha=0` -- it crops to pixels valid in BOTH cameras,
which is correct for depth and costs about 20 degrees of field -- and it aligns
to the right camera with R1. Neither is wanted for a single camera feeding a
policy, so this uses R = identity and its own projection, and takes the field
straight off the sensor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class Pinhole:
    """The virtual camera the checkpoint expects. Square pixels: training
    rendered them square, whatever the sensor's aspect happens to be."""

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

    @property
    def matrix(self) -> np.ndarray:
        return np.array([[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]])

    def half_angles_deg(self) -> tuple[float, float]:
        return (math.degrees(math.atan(min(self.cx, self.width - self.cx) / self.fx)),
                math.degrees(math.atan(min(self.cy, self.height - self.cy) / self.fy)))


@dataclass(frozen=True)
class Lens:
    """The source camera as its CameraInfo describes it: K, D and the size they
    were measured at. fx != fy is normal here -- the driver publishes 1280x720
    squashed into 640x480, so the pixels are not square."""

    k: np.ndarray
    d: np.ndarray
    width: int
    height: int

    @classmethod
    def from_camera_info(cls, k: list[float], d: list[float], width: int, height: int) -> Lens:
        return cls(np.asarray(k, float).reshape(3, 3), np.asarray(d, float).reshape(1, -1),
                   int(width), int(height))

    def scaled_to(self, width: int, height: int) -> Lens:
        """The same lens described at a different image size. D is
        dimensionless (it acts on normalised coordinates), so only K scales."""
        if (width, height) == (self.width, self.height):
            return self
        sx, sy = width / self.width, height / self.height
        k = self.k.copy()
        k[0, 0] *= sx
        k[0, 2] *= sx
        k[1, 1] *= sy
        k[1, 2] *= sy
        return Lens(k, self.d, width, height)

    def half_angles_deg(self) -> tuple[float, float]:
        """True half-angles, traced through the distortion model rather than
        assumed from K -- a pinhole formula understates a wide lens by degrees."""
        w, h = self.width, self.height
        pts = np.array([[[w / 2, 0]], [[w / 2, h - 1]], [[0, h / 2]], [[w - 1, h / 2]]], float)
        n = cv2.undistortPoints(pts, self.k, self.d).reshape(-1, 2)
        a = np.degrees(np.arctan(np.abs(n).max(axis=1)))
        return float(min(a[2], a[3])), float(min(a[0], a[1]))


def undistort_maps(src: Lens, dst: Pinhole) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(map_x, map_y, valid) for cv2.remap, each (dst.height, dst.width).

    R = identity on purpose: the stereo rectification rotation exists to align
    epipolar lines with the other camera, and a policy looking through one
    camera wants the sensor's own axis.
    """
    map_x, map_y = cv2.initUndistortRectifyMap(
        src.k, src.d, np.eye(3), dst.matrix, (dst.width, dst.height), cv2.CV_32FC1)
    inside = (map_x >= 0) & (map_x <= src.width - 1) & (map_y >= 0) & (map_y <= src.height - 1)
    # Point uncovered pixels off-image so remap blanks them, rather than
    # sampling an edge texel and inventing content the policy never saw.
    return (np.where(inside, map_x, -1.0).astype(np.float32),
            np.where(inside, map_y, -1.0).astype(np.float32), inside)


def coverage(src: Lens, dst: Pinhole) -> float:
    """Fraction of the virtual view the lens actually reaches."""
    return float(undistort_maps(src, dst)[2].mean())
