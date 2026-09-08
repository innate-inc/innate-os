#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
CameraProvider – lightweight ROS 2 node that subscribes to camera topics
in its own spin thread, storing raw compressed bytes.

Runs independently of the main executor so camera callbacks are never
starved by long-running action-server work.  Callbacks store the raw
bytes; consumers (skills/robot_state.py) wrap them lazily.

Subscriptions are created on-demand via start()/stop() so the node
consumes zero CPU when no skill needs camera data.
"""

import threading

import numpy as np
import rclpy
import rclpy.executors
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image

# Depth decoding lives here now: the brain's CameraCapture is JPEG-only.
_DEPTH_DTYPES = {"16UC1": np.uint16, "mono16": np.uint16, "32FC1": np.float32, "mono32": np.float32}


class CameraProvider(Node):
    """Subscribe to camera topics in a dedicated background thread.

    Raw compressed bytes are stored on every callback (cheap memcpy).
    Base64 strings are computed lazily via properties so the cost is
    only paid when a consumer actually reads the value.

    Call start() before reading camera data and stop() when done.
    """

    _IMAGE_QOS = QoSProfile(
        reliability=QoSReliabilityPolicy.BEST_EFFORT,
        history=QoSHistoryPolicy.KEEP_LAST,
        depth=1,
    )

    # Each skill that needs the camera builds its own provider, so the node name
    # must be unique — otherwise rcl warns "Publisher already registered for
    # provided node name" for every collision.
    _instance_count = 0

    def __init__(self):
        CameraProvider._instance_count += 1
        super().__init__(f"camera_subscriber_{CameraProvider._instance_count}")

        self._main_camera_raw: bytes | None = None
        self._wrist_camera_raw: bytes | None = None
        self._depth_msg: Image | None = None

        self._main_sub = None
        self._wrist_sub = None
        self._depth_sub = None
        self._executor: rclpy.executors.SingleThreadedExecutor | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        # start()/stop() are refcounted: a chained child that needs the camera
        # must not tear it down on exit while its parent still does
        self._users = 0
        # per-feed refcounts, so a feed only a nested child declared stops
        # streaming when the child ends instead of for the parent's whole run
        self._feed_users = {"main": 0, "wrist": 0, "depth": 0}

    # ---- lifecycle ----

    def start(self, feeds=("main", "wrist", "depth")):
        """Create subscriptions for ``feeds`` ("main"/"wrist"/"depth") and
        begin spinning in a background thread.

        Only the requested feeds are subscribed — raw depth in particular is
        ~600 KB/frame uncompressed, not worth streaming for a skill that never
        declared it. A feed a nested caller adds while already running gets
        its subscription on demand (creating on a spinning node is safe;
        destroying is what races the executor)."""
        self._users += 1
        for feed in feeds:
            if feed in self._feed_users:
                self._feed_users[feed] += 1
        if "main" in feeds and self._main_sub is None:
            self._main_sub = self.create_subscription(
                CompressedImage,
                "/mars/main_camera/left/image_raw/compressed",
                self._main_camera_cb,
                self._IMAGE_QOS,
            )
        if "wrist" in feeds and self._wrist_sub is None:
            self._wrist_sub = self.create_subscription(
                CompressedImage,
                "/mars/arm/image_raw/compressed",
                self._wrist_camera_cb,
                self._IMAGE_QOS,
            )
        if "depth" in feeds and self._depth_sub is None:
            self._depth_sub = self.create_subscription(
                Image,
                "/mars/main_camera/depth/image_rect_raw",
                self._depth_cb,
                self._IMAGE_QOS,
            )
        if self._running:
            return
        self._start_spin()
        self._running = True
        self.get_logger().info("Camera subscriptions started")

    def stop(self, feeds=("main", "wrist", "depth")):
        """Release ``feeds`` and stop the background thread when unused.

        Refcounted with start(), per feed and overall: only the last
        outstanding user stops the node, but a feed only this caller needed
        (a nested child's depth, say) is dropped right away rather than
        streaming for the rest of an enclosing skill's run.
        """
        if not self._running:
            return
        self._users = max(0, self._users - 1)
        for feed in feeds:
            if feed in self._feed_users:
                self._feed_users[feed] = max(0, self._feed_users[feed] - 1)
        if self._users:
            self._drop_unused_feeds()
            return
        self._stop_spin()
        for sub in (self._main_sub, self._wrist_sub, self._depth_sub):
            if sub is not None:
                self.destroy_subscription(sub)
        self._main_sub = None
        self._wrist_sub = None
        self._depth_sub = None
        self._main_camera_raw = None
        self._wrist_camera_raw = None
        self._depth_msg = None
        self._feed_users = dict.fromkeys(self._feed_users, 0)
        self._running = False
        self.get_logger().info("Camera subscriptions stopped")

    def _start_spin(self):
        self._executor = rclpy.executors.SingleThreadedExecutor()
        self._executor.add_node(self)
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _stop_spin(self):
        if self._executor is not None:
            self._executor.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._executor = None

    def _drop_unused_feeds(self):
        """Destroy subscriptions whose last user left while others remain.

        Destroying an entity under a spinning executor races it (#497), so
        park the spin thread first, drop the dead subscriptions, and resume —
        the surviving feeds miss at most one frame interval.
        """
        dead = [
            feed
            for feed, sub in (("main", self._main_sub), ("wrist", self._wrist_sub), ("depth", self._depth_sub))
            if sub is not None and not self._feed_users[feed]
        ]
        if not dead:
            return
        self._stop_spin()
        if "main" in dead and self._main_sub is not None:
            self.destroy_subscription(self._main_sub)
            self._main_sub = None
            self._main_camera_raw = None
        if "wrist" in dead and self._wrist_sub is not None:
            self.destroy_subscription(self._wrist_sub)
            self._wrist_sub = None
            self._wrist_camera_raw = None
        if "depth" in dead and self._depth_sub is not None:
            self.destroy_subscription(self._depth_sub)
            self._depth_sub = None
            self._depth_msg = None
        self._start_spin()
        self.get_logger().info(f"Camera feeds dropped: {', '.join(dead)}")

    # ---- callbacks (as cheap as possible) ----

    def _spin(self):
        if self._executor is None:
            return
        try:
            self._executor.spin()
        except Exception:
            pass

    def _main_camera_cb(self, msg: CompressedImage):
        self._main_camera_raw = bytes(msg.data)

    def _wrist_camera_cb(self, msg: CompressedImage):
        self._wrist_camera_raw = bytes(msg.data)

    def _depth_cb(self, msg: Image):
        self._depth_msg = msg

    # ---- frame properties ----

    @property
    def last_main_camera_jpeg(self) -> bytes | None:
        """The latest main camera frame as raw JPEG bytes, or None."""
        return self._main_camera_raw

    @property
    def last_wrist_camera_jpeg(self) -> bytes | None:
        """The latest wrist camera frame as raw JPEG bytes, or None."""
        return self._wrist_camera_raw

    @property
    def last_depth_image(self) -> "np.ndarray | None":
        """Return the latest depth frame as a (height, width) numpy array, or
        None. Dtype follows the sensor encoding (uint16 mm or float32 m);
        frombuffer is a view, so this stays cheap on every read."""
        msg = self._depth_msg
        if msg is None:
            return None
        dtype = _DEPTH_DTYPES.get(msg.encoding)
        if dtype is None:
            self.get_logger().warn(f"Unexpected depth encoding: {msg.encoding}")
            return None
        try:
            return np.frombuffer(msg.data, dtype=dtype).reshape((msg.height, msg.width))
        except ValueError:
            # Padded/truncated frame (data length ≠ height*width*itemsize).
            # This property feeds the pre-run state wait and the 50 Hz update
            # thread — a malformed frame must read as "no frame", not raise
            # out of the skills server.
            self.get_logger().warn(
                f"Depth frame does not match {msg.height}x{msg.width} {msg.encoding} (len={len(msg.data)})"
            )
            return None

    # ---- cleanup ----

    def shutdown(self):
        """Process teardown: force the full stop whatever the refcount.

        A run still in flight (or a leaked count) holds ``_users`` above 1;
        the refcounted stop() would only decrement, leaving the spin thread
        alive into rclpy.shutdown() — live entities there SIGABRT rmw_zenoh.
        """
        self._users = min(self._users, 1)
        self.stop()
