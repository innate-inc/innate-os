#!/usr/bin/env python3
"""
CameraProvider – lightweight ROS 2 node that subscribes to camera topics
in its own spin thread, storing raw compressed bytes.

Runs independently of the main executor so camera callbacks are never
starved by long-running action-server work.  Base64 encoding is deferred
to property access so the callback stays as fast as possible.
"""

import base64
import threading
import time

import numpy as np
import rclpy
import rclpy.executors
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image


class CameraProvider(Node):
    """Subscribe to camera topics in a dedicated background thread.

    Raw compressed bytes are stored on every callback (cheap memcpy).
    Base64 strings are computed lazily via properties so the cost is
    only paid when a consumer actually reads the value.
    """

    def __init__(
        self,
        main_camera_topic: str = "/mars/main_camera/image/compressed",
        wrist_camera_topic: str = "/mars/arm/image_raw/compressed",
        depth_image_topic: str = "/depth/image_raw",
    ):
        super().__init__("camera_subscriber")

        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
        )

        # Raw JPEG bytes (no decode, no base64) + timestamps
        self._main_camera_raw: bytes | None = None
        self._wrist_camera_raw: bytes | None = None
        self._depth_image_array: np.ndarray | None = None
        self._depth_image_metadata: dict | None = None
        self._main_camera_time: float = 0.0
        self._wrist_camera_time: float = 0.0
        self._depth_image_time: float = 0.0

        self.create_subscription(
            CompressedImage,
            main_camera_topic,
            self._main_camera_cb,
            image_qos,
        )
        self.create_subscription(
            CompressedImage,
            wrist_camera_topic,
            self._wrist_camera_cb,
            image_qos,
        )
        self.create_subscription(
            Image,
            depth_image_topic,
            self._depth_image_cb,
            image_qos,
        )

        # Background spin
        self._executor = rclpy.executors.SingleThreadedExecutor()
        self._executor.add_node(self)
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    # ---- callbacks (as cheap as possible) ----

    def _spin(self):
        try:
            self._executor.spin()
        except Exception:
            pass

    def _main_camera_cb(self, msg: CompressedImage):
        self._main_camera_raw = bytes(msg.data)
        self._main_camera_time = time.time()

    def _wrist_camera_cb(self, msg: CompressedImage):
        self._wrist_camera_raw = bytes(msg.data)
        self._wrist_camera_time = time.time()

    def _depth_image_cb(self, msg: Image):
        depth_array = self._decode_depth_image(msg)
        if depth_array is None:
            return

        self._depth_image_array = depth_array
        self._depth_image_metadata = {
            "encoding": msg.encoding,
            "height": int(msg.height),
            "width": int(msg.width),
            "step": int(msg.step),
            "is_bigendian": int(msg.is_bigendian),
            "frame_id": msg.header.frame_id,
            "stamp": {
                "sec": int(msg.header.stamp.sec),
                "nanosec": int(msg.header.stamp.nanosec),
            },
        }
        self._depth_image_time = time.time()

    @staticmethod
    def _decode_depth_image(msg: Image) -> np.ndarray | None:
        encoding = msg.encoding.upper()
        if encoding in ["16UC1", "MONO16"]:
            dtype = np.uint16
        elif encoding in ["32FC1", "MONO32"]:
            dtype = np.float32
        elif encoding in ["8UC1", "MONO8"]:
            dtype = np.uint8
        else:
            return None

        bytes_per_pixel = np.dtype(dtype).itemsize
        expected_row_bytes = int(msg.width) * bytes_per_pixel
        if int(msg.step) < expected_row_bytes:
            return None

        raw = np.frombuffer(msg.data, dtype=np.uint8)
        expected_total_bytes = int(msg.height) * int(msg.step)
        if raw.size != expected_total_bytes:
            return None

        row_major = raw.reshape((int(msg.height), int(msg.step)))[:, :expected_row_bytes]
        return np.ascontiguousarray(row_major).view(dtype).reshape((int(msg.height), int(msg.width)))

    # ---- lazy base64 properties ----

    @property
    def last_main_camera_b64(self) -> str | None:
        """Return the latest main camera frame as a base64 string, or None."""
        raw = self._main_camera_raw
        if raw is None:
            return None
        return base64.b64encode(raw).decode("utf-8")

    @property
    def last_wrist_camera_b64(self) -> str | None:
        """Return the latest wrist camera frame as a base64 string, or None."""
        raw = self._wrist_camera_raw
        if raw is None:
            return None
        return base64.b64encode(raw).decode("utf-8")

    @property
    def last_main_camera_time(self) -> float:
        return self._main_camera_time

    @property
    def last_wrist_camera_time(self) -> float:
        return self._wrist_camera_time

    @property
    def last_depth_state(self) -> dict | None:
        """Return latest depth frame + metadata for skill state injection, or None."""
        if self._depth_image_array is None or self._depth_image_metadata is None:
            return None

        depth_state = dict(self._depth_image_metadata)
        depth_state["array"] = self._depth_image_array.copy()
        return depth_state

    @property
    def last_depth_image_time(self) -> float:
        return self._depth_image_time

    # ---- raw access (for saving directly to file without base64 round-trip) ----

    @property
    def last_main_camera_raw(self) -> bytes | None:
        """Return the latest main camera JPEG bytes, or None."""
        return self._main_camera_raw

    @property
    def last_wrist_camera_raw(self) -> bytes | None:
        """Return the latest wrist camera JPEG bytes, or None."""
        return self._wrist_camera_raw

    # ---- cleanup ----

    def shutdown(self):
        self._executor.shutdown()
        self._thread.join(timeout=2.0)
