#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Teleop video recorder.

/video_recorder/start and /video_recorder/stop (std_srvs/Trigger) bracket an
H.264 MP4 capture of the camera topics into <INNATE_OS_ROOT>/data/recordings/.
Frames are written on a fixed-rate timer (latest frame per tick, duplicated on
stalls) so the file plays back in real time regardless of camera or subscriber
drops. Each recording also gets everything a 3D reconstruction needs later:
a rosbag of odometry / TF / joints / lidar, per-frame ROS timestamps tying
video frames to those poses, a recording_metadata.json (streams, timing,
camera intrinsics when calibration is loaded) and a robot.urdf snapshot.
"""

import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

import cv2
import numpy as np
import rclpy
import rosbag2_py
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image, JointState, LaserScan
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger
from tf2_msgs.msg import TFMessage

CAMERA_TOPICS = {
    # Full-res side-by-side stereo pair, left eye in the left half
    # (subscriber-gated in the driver, so it only flows while recording).
    # The 640x480 /left topic stays untouched for policies, datasets, teleop.
    "main": "/mars/main_camera/stereo",
    "arm": "/mars/arm/image_raw",
}
# Intrinsics published only while a stereo calibration is loaded; captured
# into the metadata sidecar when present.
CAMERA_INFO_TOPICS = [
    "/mars/main_camera/left/camera_info",
    "/mars/main_camera/right/camera_info",
]
# Pose/sensor topics bagged alongside the videos for 3D reconstruction:
# odometry + TF give the camera trajectory (tf_static carries the camera
# extrinsics chain, joint_states the head tilt), /scan adds lidar geometry.
# Topics without a live publisher just record zero messages.
BAG_TOPICS = {
    "/odom": ("nav_msgs/msg/Odometry", Odometry),
    "/mapping_pose": ("nav_msgs/msg/Odometry", Odometry),
    "/amcl_pose": ("geometry_msgs/msg/PoseWithCovarianceStamped", PoseWithCovarianceStamped),
    "/tf": ("tf2_msgs/msg/TFMessage", TFMessage),
    "/tf_static": ("tf2_msgs/msg/TFMessage", TFMessage),
    "/joint_states": ("sensor_msgs/msg/JointState", JointState),
    "/scan": ("sensor_msgs/msg/LaserScan", LaserScan),
}

# Write-timer rate. Streams whose camera publishes slower record at their own
# rate below — encoding duplicated frames buys nothing.
FPS = 30.0
# Main records below its 15 fps camera rate: the raw-BGR pipe + NVJPG chain
# sustains ~12 fps at 3840x1080 under full robot load, so 15 silently drops
# frames (measured 81% realtime); 10 holds with real margin.
RECORD_FPS = {"main": 10.0}
# 1080p records via the NVJPG hardware encoder (browsers can't play MJPEG-in-MP4,
# but VLC/QuickTime/editors can); everything else via x264, which plays anywhere.
STREAM_CODEC = {"main": "mjpeg"}


def image_to_bgr(msg: Image) -> np.ndarray | None:
    if msg.encoding not in ("bgr8", "rgb8"):
        return None
    data = np.frombuffer(msg.data, dtype=np.uint8)
    frame = data.reshape(msg.height, msg.step)[:, : msg.width * 3].reshape(msg.height, msg.width, 3)
    if msg.encoding == "rgb8":
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return frame


def camera_info_to_dict(msg: CameraInfo) -> dict:
    return {
        "frame_id": msg.header.frame_id,
        "width": msg.width,
        "height": msg.height,
        "distortion_model": msg.distortion_model,
        "d": list(msg.d),
        "k": list(msg.k),
        "r": list(msg.r),
        "p": list(msg.p),
    }


class GstMp4Writer:
    """MP4 writer: raw BGR frames piped to a gst-launch subprocess.

    The Orin Nano has no NVENC, and OpenCV's mp4v encoder tops out below
    1080p30 (measured ~20 Hz), which silently time-compressed recordings.
    1080p goes through the NVJPG hardware encoder (mjpeg): measured 2.5x
    real-time under full robot load, and it never backpressures the write
    timer the way software x264 at 1080p does (which halved every stream's
    rate). Small streams use x264 (h264), which plays everywhere.
    """

    def __init__(self, path: str, fps: float, width: int, height: int, codec: str) -> None:
        if codec == "mjpeg":
            encode = ["nvjpegenc", "quality=85", "!", "qtmux"]
        else:
            kbps = max(1500, int(width * height * fps * 0.25 / 1000))  # ~0.25 bit/px
            encode = [
                "x264enc", "speed-preset=ultrafast", f"bitrate={kbps}", f"key-int-max={int(fps * 4)}",
                "!", "h264parse", "!", "mp4mux",
            ]
        args = (
            ["gst-launch-1.0", "-q", "fdsrc", "fd=0", "!"]
            + ["rawvideoparse", "format=bgr", f"width={width}", f"height={height}", f"framerate={int(fps)}/1", "!"]
            + ["queue", "max-size-buffers=8", "!", "videoconvert", "n-threads=4", "!"]
            + ["video/x-raw,format=I420", "!", "queue", "max-size-buffers=8", "!"]
            + encode
            + ["!", "filesink", f"location={path}"]
        )
        self._proc = subprocess.Popen(args, stdin=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        # Contiguous frames go straight from the numpy buffer; 12 MB stereo
        # frames make a tobytes() copy measurable.
        self._proc.stdin.write(frame.data if frame.flags.c_contiguous else frame.tobytes())

    def release(self) -> None:
        try:
            self._proc.stdin.close()  # EOS -> mp4mux finalizes the file
        except OSError:
            pass
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class VideoRecorder(Node):
    def __init__(self) -> None:
        super().__init__("video_recorder")
        root = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))
        self.recordings_root = os.path.join(root, "data", "recordings")
        try:
            sha = subprocess.run(
                ["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
            )
            self.git_sha: str | None = sha.stdout.strip() or None
        except OSError:
            self.git_sha = None

        self.output_dir: str | None = None
        self.subs: list = []
        self.timer = None
        self.tick = 0
        self.started_at: str | None = None
        self.start_monotonic = 0.0
        self.latest: dict[str, np.ndarray] = {}
        self.writers: dict[str, GstMp4Writer] = {}
        self.frame_counts: dict[str, int] = {}
        self.camera_infos: dict[str, dict] = {}
        self.robot_description: str | None = None
        self.bag_writer: rosbag2_py.SequentialWriter | None = None
        self.bag_counts: dict[str, int] = {}
        self.frame_stamps: dict[str, list] = {}

        self.create_service(Trigger, "/video_recorder/start", self.handle_start)
        self.create_service(Trigger, "/video_recorder/stop", self.handle_stop)

        # robot_state_publisher latches the URDF; keep the latest for the
        # metadata sidecar.
        latched_qos = QoSProfile(depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(String, "/robot_description", self.on_robot_description, latched_qos)

        # Latched so the webapp sees the current state on (re)connect, plus a
        # 1 Hz heartbeat so a client holding a stale value (reconnect races,
        # node restarts mid-recording) converges to the robot's real state.
        status_qos = QoSProfile(depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        self.status_pub = self.create_publisher(Bool, "/video_recorder/status", status_qos)
        self.publish_status()
        self.create_timer(1.0, self.publish_status)

    def publish_status(self) -> None:
        self.status_pub.publish(Bool(data=self.output_dir is not None))

    def on_robot_description(self, msg: String) -> None:
        self.robot_description = msg.data

    def handle_start(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if self.output_dir is not None:
            response.success = False
            response.message = f"Already recording to {self.output_dir}"
            return response

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(self.recordings_root, f"video_{stamp}")
        os.makedirs(self.output_dir, exist_ok=True)
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.start_monotonic = time.monotonic()
        self.tick = 0

        for name, topic in CAMERA_TOPICS.items():
            self.subs.append(
                self.create_subscription(
                    Image,
                    topic,
                    lambda msg, name=name: self.on_frame(name, msg),
                    qos_profile_sensor_data,
                )
            )
        for topic in CAMERA_INFO_TOPICS:
            self.subs.append(
                self.create_subscription(
                    CameraInfo,
                    topic,
                    lambda msg, topic=topic: self.camera_infos.__setitem__(topic, camera_info_to_dict(msg)),
                    qos_profile_sensor_data,
                )
            )
        self.bag_writer = rosbag2_py.SequentialWriter()
        self.bag_writer.open(
            rosbag2_py.StorageOptions(uri=os.path.join(self.output_dir, "sensors"), storage_id="sqlite3"),
            rosbag2_py.ConverterOptions(input_serialization_format="cdr", output_serialization_format="cdr"),
        )
        for topic, (type_name, msg_cls) in BAG_TOPICS.items():
            self.bag_writer.create_topic(
                rosbag2_py.TopicMetadata(name=topic, type=type_name, serialization_format="cdr")
            )
            self.bag_counts[topic] = 0
            # tf_static is latched; a volatile sub would miss the transforms
            # published before recording started.
            qos = (
                QoSProfile(depth=10, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
                if topic == "/tf_static"
                else qos_profile_sensor_data
            )
            self.subs.append(
                self.create_subscription(
                    msg_cls, topic, lambda msg, topic=topic: self.on_bag_msg(topic, msg), qos, raw=True
                )
            )

        # Writes drain on their own thread per stream: a pipe stall (encoder
        # hiccup) must never stall the executor, and a 12 MB stereo write must
        # never queue the arm camera's frames behind it.
        self.write_qs = {name: queue.Queue(maxsize=8) for name in CAMERA_TOPICS}
        self.writer_threads = {
            name: threading.Thread(target=self.drain_writes, args=(name,), daemon=True) for name in CAMERA_TOPICS
        }
        for t in self.writer_threads.values():
            t.start()
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
        duration = time.monotonic() - self.start_monotonic
        self.output_dir = None
        self.destroy_timer(self.timer)
        self.timer = None
        for sub in self.subs:
            self.destroy_subscription(sub)
        self.subs.clear()
        self.bag_writer = None  # destructor flushes and closes the bag
        for q in self.write_qs.values():
            q.put(None)
        for t in self.writer_threads.values():
            t.join(timeout=15)
        saved = sorted(self.writers)
        streams = {
            name: {
                "file": f"{name}.mp4",
                "topic": CAMERA_TOPICS[name],
                "width": self.latest[name][0].shape[1],
                "height": self.latest[name][0].shape[0],
                "fps": RECORD_FPS.get(name, FPS),
                "codec": STREAM_CODEC.get(name, "h264"),
                "frames": self.frame_counts.get(name, 0),
            }
            for name in saved
        }
        for writer in self.writers.values():
            writer.release()
        self.writers.clear()
        self.latest.clear()
        self.frame_counts.clear()
        self.publish_status()

        if not saved:
            shutil.rmtree(output_dir, ignore_errors=True)
            response.success = False
            response.message = "No camera frames received; nothing saved"
        else:
            self.write_metadata(output_dir, duration, streams)
            response.success = True
            response.message = f"Saved {', '.join(f'{name}.mp4' for name in saved)} in {output_dir}"
        self.camera_infos.clear()
        self.bag_counts = {}
        self.frame_stamps = {}
        self.get_logger().info(response.message)
        return response

    def write_metadata(self, output_dir: str, duration: float, streams: dict) -> None:
        metadata = {
            "hostname": socket.gethostname(),
            "innate_os_git": self.git_sha,
            "started": self.started_at,
            "duration_s": round(duration, 3),
            "streams": streams,
            # Intrinsics are for the published left/right topics at their
            # native resolution; main.mp4 is the side-by-side stereo pair
            # (left eye = left half) — scale fx, fy, cx, cy by the per-eye
            # resolution ratio to apply them.
            "camera_info": self.camera_infos
            or "unavailable: no stereo calibration loaded, camera_info topics were silent",
            # Rosbag with the pose/sensor topics; <name>_frames.csv maps each
            # video frame index to the ROS stamp of the image it came from.
            "bag": {"path": "sensors", "message_counts": self.bag_counts},
        }
        with open(os.path.join(output_dir, "recording_metadata.json"), "w") as f:
            json.dump(metadata, f, indent=2)
        for name, stamps in self.frame_stamps.items():
            with open(os.path.join(output_dir, f"{name}_frames.csv"), "w") as f:
                f.write("frame,stamp_ns\n")
                f.writelines(f"{i},{ns}\n" for i, ns in enumerate(stamps))
        if self.robot_description:
            with open(os.path.join(output_dir, "robot.urdf"), "w") as f:
                f.write(self.robot_description)

    def on_bag_msg(self, topic: str, raw: bytes) -> None:
        if self.bag_writer is None:
            return
        self.bag_writer.write(topic, raw, self.get_clock().now().nanoseconds)
        self.bag_counts[topic] += 1

    def on_frame(self, name: str, msg: Image) -> None:
        frame = image_to_bgr(msg)
        if frame is None:
            return
        self.latest[name] = (frame, msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec)

    def write_tick(self) -> None:
        self.tick += 1
        output_dir = self.output_dir
        for name, (frame, stamp_ns) in list(self.latest.items()):
            step = max(1, round(FPS / RECORD_FPS.get(name, FPS)))
            if self.tick % step:
                continue
            try:
                self.write_qs[name].put_nowait((frame, stamp_ns, output_dir))
            except queue.Full:
                self.get_logger().warning(f"{name}: write queue full, dropping a frame", throttle_duration_sec=5.0)

    def drain_writes(self, name: str) -> None:
        while True:
            item = self.write_qs[name].get()
            if item is None:
                return
            frame, stamp_ns, output_dir = item
            writer = self.writers.get(name)
            if writer is None:
                height, width = frame.shape[:2]
                path = os.path.join(output_dir, f"{name}.mp4")
                writer = GstMp4Writer(path, RECORD_FPS.get(name, FPS), width, height, STREAM_CODEC.get(name, "h264"))
                self.writers[name] = writer
            try:
                writer.write(frame)
                self.frame_counts[name] = self.frame_counts.get(name, 0) + 1
                self.frame_stamps.setdefault(name, []).append(stamp_ns)
            except OSError:
                self.get_logger().error(f"{name} encoder pipeline died; dropping stream")
                self.writers.pop(name).release()


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
