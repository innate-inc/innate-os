# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing occupancy-grid map. ROS-free on purpose (numpy only)."""

import base64
import math
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

import numpy as np

from brain_client.state.dictcompat import LegacyMapping


@dataclass(frozen=True)
class Map(LegacyMapping):
    """The occupancy-grid map, read via ``self.map`` in skills.

    ``grid`` is the useful part: a (height, width) int8 array where -1 is
    unknown, 0 free and 100 occupied. Cell (row, col) covers the world
    point ``(origin_x + col * resolution, origin_y + row * resolution)``
    (map frame, before origin rotation, which is 0 for MARS maps).
    """

    resolution: float
    """Cell edge length in meters."""
    width: int
    """Grid width in cells."""
    height: int
    """Grid height in cells."""
    origin_x: float
    """World X of cell (0, 0)'s corner, map frame."""
    origin_y: float
    """World Y of cell (0, 0)'s corner, map frame."""
    origin_theta: float = 0.0
    """Grid rotation in radians (0 for MARS maps)."""
    stamp: float = 0.0
    """Map timestamp in seconds (ROS time)."""
    frame_id: str = "map"
    raw_source: Any = field(default=None, repr=False, compare=False)
    """The nav_msgs/OccupancyGrid message this was built from; provenance
    for the lazy ``grid``/legacy views. Excluded from ==/hash."""
    keepout_source: Any = field(default=None, repr=False, compare=False)
    """Optional matching keepout OccupancyGrid, composited only for skill reads."""

    @cached_property
    def grid(self) -> "np.ndarray | None":
        """(height, width) int8 occupancy values: -1 unknown, 0 free,
        100 occupied. Built lazily so map reads cost nothing until a skill
        actually looks at the cells."""
        if self.raw_source is None:
            return None
        grid = np.array(self.raw_source.data, dtype=np.int8).reshape((self.height, self.width))
        keepout = self.keepout_grid
        if keepout is None:
            return grid
        return np.where(keepout >= 50, 100, grid).astype(np.int8)

    @cached_property
    def keepout_grid(self) -> "np.ndarray | None":
        """Matching keepout cells alone, for direct-motion safety checks."""
        mask = self.keepout_source
        if mask is None or not self._matches(mask):
            return None
        return np.array(mask.data, dtype=np.int8).reshape((self.height, self.width))

    def _matches(self, mask: Any) -> bool:
        """A retained mask from another map must never contaminate this map."""
        try:
            source = self.raw_source
            return (
                mask.header.frame_id == source.header.frame_id
                and mask.info.width == source.info.width
                and mask.info.height == source.info.height
                and math.isclose(mask.info.resolution, source.info.resolution, abs_tol=1e-9)
                and math.isclose(mask.info.origin.position.x, source.info.origin.position.x, abs_tol=1e-6)
                and math.isclose(mask.info.origin.position.y, source.info.origin.position.y, abs_tol=1e-6)
                and mask.info.map_load_time.sec == source.info.map_load_time.sec
                and mask.info.map_load_time.nanosec == source.info.map_load_time.nanosec
                and len(mask.data) == self.width * self.height
            )
        except (AttributeError, TypeError):
            return False

    # --- legacy dict compatibility ---------------------------------------
    # LAST_MAP injected a header/info/data_b64 dict before ambient state.

    _legacy_hint = "the Map attributes (map.grid, map.resolution, map.origin_x, ...)"

    @cached_property
    def _legacy_dict(self) -> dict:
        sec = int(self.stamp)
        source = self.raw_source
        load_time = (
            {"sec": source.info.map_load_time.sec, "nanosec": source.info.map_load_time.nanosec}
            if source is not None
            else {"sec": 0, "nanosec": 0}
        )
        origin_z = source.info.origin.position.z if source is not None else 0.0
        half = self.origin_theta / 2.0
        grid = self.grid
        return {
            "header": {
                "stamp": {"sec": sec, "nanosec": int(round((self.stamp - sec) * 1e9))},
                "frame_id": self.frame_id,
            },
            "info": {
                "map_load_time": load_time,
                "resolution": self.resolution,
                "width": self.width,
                "height": self.height,
                "origin": {
                    "position": {"x": self.origin_x, "y": self.origin_y, "z": origin_z},
                    "orientation": {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)},
                    "yaw_degrees": math.degrees(self.origin_theta),
                },
            },
            "data_b64": base64.b64encode(grid.tobytes()).decode("utf-8") if grid is not None else "",
        }
