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
import time
from collections import deque
from dataclasses import replace

import numpy as np
import rclpy
import rclpy.executors
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from std_msgs.msg import Int64
from tf2_ros import Buffer, TransformException, TransformListener

from .rgbd import RgbdObservation, decode_depth, stamp_ns


class _CaptureTransforms(Buffer):
    """Discard queued dynamic transforms from before stream invalidation."""

    def __init__(self, minimum_stamp):
        super().__init__(cache_time=Duration(seconds=3))
        self._minimum_stamp = minimum_stamp

    def set_transform(self, transform, authority):
        if stamp_ns(transform) >= self._minimum_stamp():
            super().set_transform(transform, authority)


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
        self._wrist_capture = None
        self._depth_msg: Image | None = None
        self._rgbd_lock = threading.Lock()
        self._rgbd_frames = {feed: deque(maxlen=8) for feed in ("main", "depth", "info")}
        self._info_sub = None
        self._epoch_sub = None
        self._stream_epoch = None
        self._rgbd_generation = 0
        self._rgbd_cached = None
        self._rgbd_capture_after_ns = 0
        self._tf_buffer = None
        self._tf_listener = None

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
        if "depth" in feeds and self._info_sub is None:
            self._info_sub = self.create_subscription(
                CameraInfo, "/mars/main_camera/left/camera_info", self._info_cb, self._IMAGE_QOS
            )
        if ({"depth", "wrist"} & set(feeds)) and self._epoch_sub is None:
            # Optional source-generation notifications invalidate retained frames.
            # Backend-specific reset topics are bound by launch remapping.
            self._epoch_sub = self.create_subscription(
                Int64,
                "/mars/main_camera/stream_epoch",
                self._epoch_cb,
                QoSProfile(depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL),
            )
        if self._main_sub is not None and self._depth_sub is not None and self._tf_listener is None:
            self._tf_buffer = _CaptureTransforms(lambda: self._rgbd_capture_after_ns)
            self._tf_listener = TransformListener(self._tf_buffer, self)
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
        self._stop_capture_transforms()
        for sub in (self._main_sub, self._wrist_sub, self._depth_sub, self._info_sub, self._epoch_sub):
            if sub is not None:
                self.destroy_subscription(sub)
        self._main_sub = None
        self._wrist_sub = None
        self._depth_sub = None
        self._main_camera_raw = None
        self._wrist_camera_raw = None
        self._wrist_capture = None
        self._depth_msg = None
        self._feed_users = dict.fromkeys(self._feed_users, 0)
        self._info_sub = None
        self._epoch_sub = None
        with self._rgbd_lock:
            self._invalidate_rgbd()
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
        if "main" in dead or "depth" in dead:
            self._stop_capture_transforms()
        if "main" in dead and self._main_sub is not None:
            self.destroy_subscription(self._main_sub)
            self._main_sub = None
            self._main_camera_raw = None
        if "wrist" in dead and self._wrist_sub is not None:
            self.destroy_subscription(self._wrist_sub)
            self._wrist_sub = None
            self._wrist_camera_raw = None
            self._wrist_capture = None
        if "depth" in dead and self._depth_sub is not None:
            self.destroy_subscription(self._depth_sub)
            self._depth_sub = None
            self._depth_msg = None
        if "depth" in dead and self._info_sub is not None:
            self.destroy_subscription(self._info_sub)
            self._info_sub = None
        if not self._feed_users["depth"] and not self._feed_users["wrist"] and self._epoch_sub is not None:
            self.destroy_subscription(self._epoch_sub)
            self._epoch_sub = None
        with self._rgbd_lock:
            if set(dead) & {"main", "depth", "wrist"}:
                self._rgbd_generation += 1
                self._wrist_capture = None
                self._wrist_camera_raw = None
                self._rgbd_cached = None
            for feed in dead:
                if feed in self._rgbd_frames:
                    self._rgbd_frames[feed].clear()
            if "depth" in dead:
                self._rgbd_frames["info"].clear()
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
        self._remember_frame("main", msg)

    def _wrist_camera_cb(self, msg: CompressedImage):
        with self._rgbd_lock:
            if stamp_ns(msg) < self._rgbd_capture_after_ns:
                return
            jpeg = bytes(msg.data)
            self._wrist_capture = (
                jpeg,
                stamp_ns(msg),
                time.monotonic(),
                self.get_clock().now().nanoseconds,
                self._rgbd_generation,
            )
            self._wrist_camera_raw = jpeg

    def _depth_cb(self, msg: Image):
        self._depth_msg = msg
        self._remember_frame("depth", msg)

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
    def wrist_capture(self):
        """Atomic JPEG/capture/receipt/generation snapshot for typed consumers."""
        with self._rgbd_lock:
            return self._wrist_capture

    def wrist_generation_is_current(self, generation):
        with self._rgbd_lock:
            return generation == self._rgbd_generation and self._wrist_capture is not None

    @property
    def last_depth_image(self) -> "np.ndarray | None":
        """Return the latest depth frame as a (height, width) numpy array, or
        None. Dtype follows the sensor encoding (uint16 mm or float32 m);
        frombuffer is a view, so this stays cheap on every read."""
        msg = self._depth_msg
        if msg is None:
            return None
        return decode_depth(msg)

    def _remember_frame(self, feed, msg):
        with self._rgbd_lock:
            self._rgbd_frames[feed].append((msg, time.monotonic()))

    @staticmethod
    def _same_calibration(a, b):
        fields = ("width", "height", "distortion_model", "binning_x", "binning_y", "roi")
        return (
            a.header.frame_id == b.header.frame_id
            and all(getattr(a, key) == getattr(b, key) for key in fields)
            and all(np.array_equal(getattr(a, key), getattr(b, key)) for key in ("k", "d", "r", "p"))
        )

    def _invalidate_rgbd(self):
        # Called with the cache lock held. In-flight pre-reset messages are also
        # excluded by capture time, even if they arrive after this notification.
        self._rgbd_generation += 1
        self._wrist_capture = None
        self._wrist_camera_raw = None
        self._rgbd_cached = None
        self._rgbd_capture_after_ns = self.get_clock().now().nanoseconds
        if self._tf_buffer is not None:
            self._tf_buffer.clear()  # dynamic history only; static extrinsics survive
        for frames in self._rgbd_frames.values():
            frames.clear()

    def _epoch_cb(self, msg):
        with self._rgbd_lock:
            if msg.data != self._stream_epoch:
                self._stream_epoch = msg.data
                self._invalidate_rgbd()

    def _info_cb(self, msg):
        with self._rgbd_lock:
            infos = self._rgbd_frames["info"]
            if infos and not self._same_calibration(infos[-1][0], msg):
                self._invalidate_rgbd()
            infos.append((msg, time.monotonic()))

    def observation_is_current(self, observation, max_age=0.5):
        """Required before reusing a retained observation after inference/reset.

        This checks sensor age and calibration/session validity, not target or
        robot motion. Those still require a current pose and visual confirmation.
        """
        with self._rgbd_lock:
            return (
                np.isfinite(max_age)
                and max_age > 0
                and observation.generation == self._rgbd_generation
                and observation.stamp_ns >= self._rgbd_capture_after_ns
                and 0 <= (self.get_clock().now().nanoseconds - observation.stamp_ns) * 1e-9 <= max_age
                and all(0 <= time.monotonic() - t <= max_age for t in observation.received_monotonic)
            )

    def _stop_capture_transforms(self):
        # Subscription destruction is only called while the executor is parked.
        if self._tf_listener is not None:
            self._tf_listener.unregister()
        self._tf_listener = self._tf_buffer = None

    def rgbd_observation(self, max_age=0.5, *, require_pose=False) -> RgbdObservation | None:
        """Newest exact-stamp calibrated triplet; subscribe to main and depth.

        Independent render/publication stamps are deliberately not matched by
        proximity. TF binds base_link to the actual capture time; no latest-pose
        fallback or stationarity inference is made. The SDK requires that pose.
        """
        with self._rgbd_lock:
            frames = {key: tuple(value) for key, value in self._rgbd_frames.items()}
            generation = self._rgbd_generation
            capture_after = self._rgbd_capture_after_ns
        if not all(frames.values()):
            return None
        latest_info = frames["info"][-1][0]
        for rgb, rgb_time in reversed(frames["main"]):
            stamp = stamp_ns(rgb)
            depth = next((pair for pair in reversed(frames["depth"]) if stamp_ns(pair[0]) == stamp), None)
            info = next((pair for pair in reversed(frames["info"]) if stamp_ns(pair[0]) == stamp), None)
            if stamp < capture_after or depth is None or info is None:
                continue
            if not self._same_calibration(info[0], latest_info):
                continue
            marker = (generation, stamp, id(rgb), id(depth[0]), id(info[0]))
            cached = self._rgbd_cached
            if cached is not None and cached[0] == marker:
                observation = cached[2]
            else:
                observation = RgbdObservation.from_messages(
                    rgb,
                    depth[0],
                    info[0],
                    (rgb_time, depth[1], info[1]),
                    now_ns=self.get_clock().now().nanoseconds,
                    now_monotonic=time.monotonic(),
                    max_age=max_age,
                    generation=generation,
                )
                if observation is not None:
                    # Retain messages with the marker so object ids cannot be
                    # recycled. Recheck age/generation on every cached read.
                    self._rgbd_cached = (marker, (rgb, depth[0], info[0]), observation)
            tf_buffer = self._tf_buffer
            if observation is not None and tf_buffer is not None:
                for frame, field in (("base_link", "base_from_optical"), ("odom", "odom_from_optical")):
                    try:
                        transform = tf_buffer.lookup_transform(
                            frame, observation.frame_id, Time(nanoseconds=stamp)
                        ).transform
                    except TransformException:
                        continue  # missing/bracketing TF is unavailable, never a latest pose
                    t, q = transform.translation, transform.rotation
                    observation = replace(observation, **{field: (t.x, t.y, t.z, q.x, q.y, q.z, q.w)})
            if require_pose and (observation is None or observation.base_from_optical is None):
                return None
            return (
                observation if observation is not None and self.observation_is_current(observation, max_age) else None
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
