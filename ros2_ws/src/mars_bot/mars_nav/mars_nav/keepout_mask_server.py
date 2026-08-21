#!/usr/bin/env python3
"""Own the editable, persistent Nav2 keepout mask without modifying /map."""

import copy
import math
import os
from pathlib import Path

import rclpy
from nav2_msgs.msg import CostmapFilterInfo
from nav2_msgs.srv import ClearEntireCostmap
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from mars_nav.keepout_mask import GridSpec, binary_mask, compatible, load_mask, map_fingerprint, save_mask

MASK_TOPIC = "/nav/keepout_filter_mask"
EDIT_TOPIC = "/nav/keepout_mask_edit"
INFO_TOPIC = "/nav/keepout_filter_info"


def _yaw(q) -> float:
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


def _spec(msg: OccupancyGrid) -> GridSpec:
    return GridSpec(
        width=msg.info.width,
        height=msg.info.height,
        resolution=msg.info.resolution,
        origin_x=msg.info.origin.position.x,
        origin_y=msg.info.origin.position.y,
        origin_yaw=_yaw(msg.info.origin.orientation),
        frame_id=msg.header.frame_id or "map",
    )


class KeepoutMaskServer(Node):
    def __init__(self):
        super().__init__("keepout_mask_server")
        latched = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL
        )
        self._mask_pub = self.create_publisher(OccupancyGrid, MASK_TOPIC, latched)
        self._info_pub = self.create_publisher(CostmapFilterInfo, INFO_TOPIC, latched)
        self.create_subscription(OccupancyGrid, "/map", self._on_map, latched)
        self.create_subscription(OccupancyGrid, EDIT_TOPIC, self._on_edit, 5)

        mars_root = Path(os.environ.get("INNATE_OS_ROOT", Path.home() / "innate-os"))
        self._storage = mars_root / "data" / "keepouts"
        self._map: OccupancyGrid | None = None
        self._spec: GridSpec | None = None
        self._map_hash = ""
        self._cells: list[int] = []
        self._clear_clients = [
            self.create_client(ClearEntireCostmap, "/navigation/global_costmap/clear_entirely_global_costmap"),
            self.create_client(ClearEntireCostmap, "/local_costmap/clear_entirely_local_costmap"),
        ]
        self._clear_timer = None
        self._publish_info()

    def _path(self) -> Path:
        return self._storage / f"{self._map_hash}.json.gz"

    def _publish_info(self) -> None:
        info = CostmapFilterInfo()
        info.header.stamp = self.get_clock().now().to_msg()
        info.header.frame_id = "map"
        info.type = 0  # nav2_costmap_2d::KEEPOUT_FILTER
        info.filter_mask_topic = MASK_TOPIC
        info.base = 0.0
        info.multiplier = 1.0
        self._info_pub.publish(info)

    def _publish_mask(self) -> None:
        if self._map is None:
            return
        msg = copy.deepcopy(self._map)
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.data = self._cells
        self._mask_pub.publish(msg)
        self._publish_info()

    def _on_map(self, msg: OccupancyGrid) -> None:
        try:
            spec = _spec(msg)
            map_hash = map_fingerprint(spec, msg.data)
        except (TypeError, ValueError) as exc:
            self.get_logger().warning(f"Ignoring malformed /map for keepout mask: {exc}")
            return
        self._map = copy.deepcopy(msg)
        self._spec = spec
        if map_hash != self._map_hash:
            self._map_hash = map_hash
            self._cells = load_mask(self._path(), map_hash, spec) or [0] * spec.cells
            marked = sum(value >= 50 for value in self._cells)
            self.get_logger().info(f"Loaded keepout mask {map_hash[:10]} ({marked} marked cells)")
        self._publish_mask()

    def _on_edit(self, msg: OccupancyGrid) -> None:
        if self._map is None or self._spec is None:
            self.get_logger().warning("Ignoring keepout edit before /map is available")
            return
        try:
            incoming = _spec(msg)
            if not compatible(incoming, self._spec):
                raise ValueError("edit geometry does not match the active navigation map")
            cells = binary_mask(msg.data, self._spec.cells)
        except (TypeError, ValueError) as exc:
            self.get_logger().warning(f"Ignoring invalid keepout edit: {exc}")
            return
        self._cells = cells
        save_mask(self._path(), self._map_hash, self._spec, cells)
        self._publish_mask()
        self._schedule_costmap_clear()

    def _schedule_costmap_clear(self) -> None:
        if self._clear_timer is not None:
            self._clear_timer.cancel()
        self._clear_timer = self.create_timer(0.2, self._clear_costmaps_once)

    def _clear_costmaps_once(self) -> None:
        if self._clear_timer is not None:
            self._clear_timer.cancel()
            self._clear_timer = None
        for client in self._clear_clients:
            if client.service_is_ready():
                client.call_async(ClearEntireCostmap.Request())


def main(args=None):
    rclpy.init(args=args)
    node = KeepoutMaskServer()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
