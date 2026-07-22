#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Teleop video recorder.

/video_recorder/start and /video_recorder/stop (std_srvs/Trigger) bracket an
MP4 capture of the camera topics into <INNATE_OS_ROOT>/data/recordings/. Frames are
written on a fixed-rate timer (latest frame per tick, duplicated on stalls) so
the file plays back in real time regardless of camera or subscriber drops.
"""

import os
import shutil
from datetime import datetime

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, qos_profile_sensor_data
from sensor_msgs.msg import Image
from std_msgs.msg import Bool
from std_srvs.srv import Trigger

CAMERA_TOPICS = {
    "main": "/mars/main_camera/left/image_raw",
    "arm": "/mars/arm/image_raw",
}
FPS = 30.0


def image_to_bgr(msg: Image) -> np.ndarray | None:
    if msg.encoding not in ("bgr8", "rgb8"):
        return None
    data = np.frombuffer(msg.data, dtype=np.uint8)
    frame = data.reshape(msg.height, msg.step)[:, : msg.width * 3].reshape(msg.height, msg.width, 3)
    if msg.encoding == "rgb8":
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return frame


class VideoRecorder(Node):
    def __init__(self) -> None:
        super().__init__("video_recorder")
        root = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))
        self.recordings_root = os.path.join(root, "data", "recordings")

        self.output_dir: str | None = None
        self.subs: list = []
        self.timer = None
        self.latest: dict[str, np.ndarray] = {}
        self.writers: dict[str, cv2.VideoWriter] = {}

        self.create_service(Trigger, "/video_recorder/start", self.handle_start)
        self.create_service(Trigger, "/video_recorder/stop", self.handle_stop)

        # Latched so the webapp sees the current state on (re)connect, plus a
        # 1 Hz heartbeat so a client holding a stale value (reconnect races,
        # node restarts mid-recording) converges to the robot's real state.
        status_qos = QoSProfile(depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        self.status_pub = self.create_publisher(Bool, "/video_recorder/status", status_qos)
        self.publish_status()
        self.create_timer(1.0, self.publish_status)

    def publish_status(self) -> None:
        self.status_pub.publish(Bool(data=self.output_dir is not None))

    def handle_start(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if self.output_dir is not None:
            response.success = False
            response.message = f"Already recording to {self.output_dir}"
            return response

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(self.recordings_root, f"video_{stamp}")
        os.makedirs(self.output_dir, exist_ok=True)

        for name, topic in CAMERA_TOPICS.items():
            self.subs.append(
                self.create_subscription(
                    Image,
                    topic,
                    lambda msg, name=name: self.on_frame(name, msg),
                    qos_profile_sensor_data,
                )
            )
        self.timer = self.create_timer(1.0 / FPS, self.write_tick)
        self.publish_status()
        self.get_logger().info(f"Recording to {self.output_dir}")

        response.success = True
        response.message = self.output_dir
        return response

    def handle_stop(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if self.output_dir is None:
            response.success = False
            response.message = "Not recording"
            return response

        output_dir = self.output_dir
        self.output_dir = None
        self.destroy_timer(self.timer)
        self.timer = None
        for sub in self.subs:
            self.destroy_subscription(sub)
        self.subs.clear()
        saved = sorted(self.writers)
        for writer in self.writers.values():
            writer.release()
        self.writers.clear()
        self.latest.clear()
        self.publish_status()

        if not saved:
            shutil.rmtree(output_dir, ignore_errors=True)
            response.success = False
            response.message = "No camera frames received; nothing saved"
        else:
            response.success = True
            response.message = f"Saved {', '.join(f'{name}.mp4' for name in saved)} in {output_dir}"
        self.get_logger().info(response.message)
        return response

    def on_frame(self, name: str, msg: Image) -> None:
        frame = image_to_bgr(msg)
        if frame is not None:
            self.latest[name] = frame

    def write_tick(self) -> None:
        for name, frame in self.latest.items():
            writer = self.writers.get(name)
            if writer is None:
                height, width = frame.shape[:2]
                path = os.path.join(self.output_dir, f"{name}.mp4")
                writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (width, height))
                self.writers[name] = writer
            writer.write(frame)


def main() -> None:
    rclpy.init()
    node = VideoRecorder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()


if __name__ == "__main__":
    main()
