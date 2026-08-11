# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure occupancy-grid rasterization for local-agent map images."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

_UNKNOWN_GRAY = 205
_FREE_GRAY = 254
_MAX_EDGE_PX = 480


@dataclass(frozen=True)
class NavigationMapImage:
    jpeg: bytes
    resolution: float
    width: int
    height: int
    origin_x: float
    origin_y: float
    origin_yaw: float
    frame_id: str

    def observation_note(self, image_number: int) -> str:
        width_m = self.width * self.resolution
        height_m = self.height * self.resolution
        cos_yaw = math.cos(self.origin_yaw)
        sin_yaw = math.sin(self.origin_yaw)
        right = (self.origin_x + width_m * cos_yaw, self.origin_y + width_m * sin_yaw)
        upper = (self.origin_x - height_m * sin_yaw, self.origin_y + height_m * cos_yaw)
        opposite = (right[0] - height_m * sin_yaw, right[1] + height_m * cos_yaw)
        return (
            f"(image {image_number} is the navigation occupancy map: image right is map-local +x "
            f"and image top is map-local +y; white is free, darker means more occupied, and unknown "
            f"is fixed light gray; {self.resolution:g} m/cell; corners in {self.frame_id or 'map'} "
            f"are lower-left=({self.origin_x:.2f},{self.origin_y:.2f}), "
            f"lower-right=({right[0]:.2f},{right[1]:.2f}), upper-left=({upper[0]:.2f},{upper[1]:.2f}), "
            f"upper-right=({opposite[0]:.2f},{opposite[1]:.2f})m; map-local yaw="
            f"{math.degrees(self.origin_yaw):.1f}deg)"
        )


def occupancy_grid_raster(data: Sequence[int], width: int, height: int) -> np.ndarray | None:
    """Render row-major occupancy cells with image-top at highest map y."""
    cell_count = width * height
    if width <= 0 or height <= 0 or cell_count <= 1 or len(data) != cell_count:
        return None
    grid = np.asarray(data, dtype=np.int16).reshape((height, width))
    raster = np.full(grid.shape, _UNKNOWN_GRAY, dtype=np.uint8)
    known = grid >= 0
    occupancy = np.clip(grid[known], 0, 100)
    raster[known] = np.rint(_FREE_GRAY * (100 - occupancy) / 100).astype(np.uint8)
    return np.flipud(raster)


def _downsample_conservatively(raster: np.ndarray, width: int, height: int) -> np.ndarray:
    """Keep the darkest source cell in each output bin so thin walls survive."""
    y_starts = np.floor(np.arange(height) * raster.shape[0] / height).astype(np.intp)
    x_starts = np.floor(np.arange(width) * raster.shape[1] / width).astype(np.intp)
    return np.minimum.reduceat(np.minimum.reduceat(raster, y_starts, axis=0), x_starts, axis=1)


def render_navigation_map(
    data: Sequence[int],
    *,
    width: int,
    height: int,
    resolution: float,
    origin_x: float,
    origin_y: float,
    origin_yaw: float,
    frame_id: str,
) -> NavigationMapImage | None:
    raster = occupancy_grid_raster(data, width, height)
    if raster is None:
        return None
    longest_edge = max(raster.shape)
    if longest_edge > _MAX_EDGE_PX:
        scale = _MAX_EDGE_PX / longest_edge
        size = (max(1, round(raster.shape[1] * scale)), max(1, round(raster.shape[0] * scale)))
        raster = _downsample_conservatively(raster, *size)
    encoded, jpeg = cv2.imencode(".jpg", raster, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not encoded:
        return None
    return NavigationMapImage(
        jpeg=jpeg.tobytes(),
        resolution=resolution,
        width=width,
        height=height,
        origin_x=origin_x,
        origin_y=origin_y,
        origin_yaw=origin_yaw,
        frame_id=frame_id,
    )
