# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Latched occupancy-map source for the local agent."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from nav_msgs.msg import OccupancyGrid
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.perception.map_image import NavigationMapImage, render_navigation_map

if TYPE_CHECKING:
    from rclpy.node import Node
    from rclpy.subscription import Subscription

_MAP_QOS = QoSProfile(
    reliability=QoSReliabilityPolicy.RELIABLE,
    history=QoSHistoryPolicy.KEEP_LAST,
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
)


class MapCapture:
    def __init__(self, node: Node, map_topic: str):
        self._map: OccupancyGrid | None = None
        self._cache: tuple[OccupancyGrid, NavigationMapImage | None] | None = None
        self._subscription: Subscription = node.create_subscription(OccupancyGrid, map_topic, self._on_map, _MAP_QOS)

    def _on_map(self, msg: OccupancyGrid) -> None:
        self._map = msg
        self._cache = None

    def latest_image(self) -> NavigationMapImage | None:
        msg = self._map
        if msg is None:
            return None
        cached = self._cache
        if cached is not None and cached[0] is msg:
            return cached[1]
        info = msg.info
        orientation = info.origin.orientation
        origin_yaw = math.atan2(
            2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
            1.0 - 2.0 * (orientation.y * orientation.y + orientation.z * orientation.z),
        )
        image = render_navigation_map(
            msg.data,
            width=info.width,
            height=info.height,
            resolution=info.resolution,
            origin_x=info.origin.position.x,
            origin_y=info.origin.position.y,
            origin_yaw=origin_yaw,
            frame_id=msg.header.frame_id,
        )
        self._cache = (msg, image)
        return image
