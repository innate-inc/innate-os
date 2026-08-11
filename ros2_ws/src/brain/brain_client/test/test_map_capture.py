# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from types import SimpleNamespace

from nav_msgs.msg import OccupancyGrid
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSReliabilityPolicy

from brain_client.perception import map_capture


class FakeNode:
    def create_subscription(self, message_type, topic, callback, qos):
        self.message_type = message_type
        self.topic = topic
        self.callback = callback
        self.qos = qos
        return SimpleNamespace()


def occupancy_grid(data: list[int]) -> OccupancyGrid:
    msg = OccupancyGrid()
    msg.header.frame_id = "map"
    msg.info.width = len(data)
    msg.info.height = 1
    msg.info.resolution = 0.05
    msg.info.origin.position.x = -1.0
    msg.info.origin.position.y = -2.0
    msg.data = data
    return msg


def test_map_capture_uses_latched_map_qos_and_requested_topic():
    node = FakeNode()

    map_capture.MapCapture(node, "/semantic_map")

    assert node.message_type is OccupancyGrid
    assert node.topic == "/semantic_map"
    assert node.qos.reliability == QoSReliabilityPolicy.RELIABLE
    assert node.qos.history == QoSHistoryPolicy.KEEP_LAST
    assert node.qos.depth == 1
    assert node.qos.durability == QoSDurabilityPolicy.TRANSIENT_LOCAL


def test_map_capture_renders_lazily_and_invalidates_the_cached_image(monkeypatch):
    node = FakeNode()
    capture = map_capture.MapCapture(node, "/map")
    render = map_capture.render_navigation_map
    calls = []

    def counting_render(*args, **kwargs):
        calls.append((args, kwargs))
        return render(*args, **kwargs)

    monkeypatch.setattr(map_capture, "render_navigation_map", counting_render)
    node.callback(occupancy_grid([0, 100]))

    first = capture.latest_image()
    assert first is not None
    assert capture.latest_image() is first
    assert len(calls) == 1

    node.callback(occupancy_grid([100, 0]))

    second = capture.latest_image()
    assert second is not None
    assert second is not first
    assert len(calls) == 2
