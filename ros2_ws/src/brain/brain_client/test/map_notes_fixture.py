# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Local ROS fixture for webapp/tests/map_notes_browser.py. No robot or model calls.

With the ROS workspace sourced, run this alongside rosbridge_websocket.
"""

import json
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger

from brain_client.memory.note_bridge import LATCHED_QOS, NoteBridge
from brain_client.memory.note_map import NoteMapRenderer
from brain_client.memory.notes import MapNotes
from brain_client.memory.store import MemoryStore


def main():
    rclpy.init()
    with tempfile.TemporaryDirectory(prefix="map-notes-ui-") as temporary:
        data = Path(temporary)
        (data / "maps").mkdir()
        grid = np.full((100, 140), 254, np.uint8)
        cv2.rectangle(grid, (8, 8), (132, 92), 0, 2)
        cv2.line(grid, (75, 8), (75, 42), 0, 2)
        cv2.line(grid, (75, 60), (75, 92), 0, 2)
        cv2.rectangle(grid, (95, 20), (120, 40), 0, 2)
        cv2.rectangle(grid, (22, 30), (48, 50), 0, 2)
        cv2.rectangle(grid, (21, 65), (50, 76), 0, 2)
        cv2.imwrite(str(data / "maps" / "home.pgm"), grid)
        (data / "maps" / "home.yaml").write_text("image: home.pgm\nresolution: 0.1\norigin: [-7, -5, 0]\n")
        memory = MemoryStore(data)
        memory.switch_map("home.yaml")
        notes = MapNotes(data, memory.snapshot, NoteMapRenderer(data))
        _, jpeg = cv2.imencode(".jpg", cv2.cvtColor(grid, cv2.COLOR_GRAY2BGR))
        regions = [
            [{"x": 660, "y": 240}, {"x": 840, "y": 240}, {"x": 840, "y": 420}, {"x": 660, "y": 420}],
            [{"x": 150, "y": 630}, {"x": 400, "y": 630}, {"x": 400, "y": 820}, {"x": 150, "y": 820}],
            [{"x": 480, "y": 415}, {"x": 600, "y": 415}, {"x": 600, "y": 610}, {"x": 480, "y": 610}],
        ]

        def seed_notes():
            for i, (title, text, point) in enumerate(
                [
                    ("Blue bowl", "On the kitchen table, within the outlined area.", (-1, 0, 0)),
                    ("Charging corner", "Dock is beside the sofa.", (-4, -3, 1.57)),
                    ("Doorway", "Keep this passage clear.", (-2, 2, 0)),
                ]
            ):
                obs = notes.observe(point, jpeg.tobytes())
                result = notes.execute(
                    "write_map_note",
                    {
                        "title": title,
                        "text": text,
                        "certainty": "observed",
                        "observation_id": obs.id,
                        "map_region": regions[i],
                    },
                    ref=obs.map_ref,
                    call_id=f"seed{i}-{time.time_ns()}",
                    observation=obs,
                )
                assert result["ok"], result

        seed_notes()
        node = Node("map_notes_ui_fixture")
        bridge = NoteBridge(node, notes)
        publishers = {
            topic: node.create_publisher(String, topic, LATCHED_QOS)
            for topic in ["/brain/memory_positions", "/nav/current_map", "/nav/current_mode", "/webrtc/active_streams"]
        }
        map_pub = node.create_publisher(OccupancyGrid, "/map", LATCHED_QOS)
        pose_pub = node.create_publisher(PoseWithCovarianceStamped, "/amcl_pose", LATCHED_QOS)
        pose = PoseWithCovarianceStamped()
        pose.header.frame_id = "map"
        pose.pose.pose.position.x = -1.0
        pose.pose.pose.orientation.w = 1.0
        message = OccupancyGrid()
        message.header.frame_id = "map"
        message.info.width, message.info.height = 140, 100
        message.info.resolution = 0.1
        message.info.origin.position.x, message.info.origin.position.y = -7.0, -5.0
        message.info.origin.orientation.w = 1.0
        message.data = np.where(np.flipud(grid) == 0, 100, 0).astype(np.int8).ravel().tolist()

        def publish():
            message.header.stamp = node.get_clock().now().to_msg()
            map_pub.publish(message)
            pose.header.stamp = message.header.stamp
            pose_pub.publish(pose)
            publishers["/webrtc/active_streams"].publish(String(data=json.dumps({"cameras": ["main"]})))
            ref = notes.map_ref()
            publishers["/brain/memory_positions"].publish(
                String(
                    data=json.dumps(
                        {
                            "map": ref["map"] if ref else "",
                            "fingerprint": ref["fingerprint"][:12] if ref else "",
                            "positions": [],
                            "stamp": time.time(),
                        }
                    )
                )
            )
            publishers["/nav/current_map"].publish(String(data=ref["map"] if ref else ""))
            publishers["/nav/current_mode"].publish(String(data="navigation"))

        node.create_timer(0.5, publish)

        def change_map(request, response):
            memory.switch_map(None if memory.snapshot().map_name else "home.yaml")
            bridge.publish()
            publish()
            response.success = True
            return response

        node.create_service(Trigger, "/test/change_map", change_map)

        def reset_notes(request, response):
            for note in notes.snapshot()["notes"]:
                notes.execute(
                    "remove_map_note",
                    {"note_id": note["id"], "expected_revision": note["revision"]},
                    ref=notes.map_ref(),
                    call_id="reset-" + str(time.time_ns()),
                )
            seed_notes()
            bridge.publish()
            response.success = True
            return response

        node.create_service(Trigger, "/test/reset_notes", reset_notes)
        print("Map-note fixture ready", flush=True)
        try:
            rclpy.spin(node)
        finally:
            node.destroy_node()
            notes.close()
            rclpy.shutdown()


if __name__ == "__main__":
    main()
