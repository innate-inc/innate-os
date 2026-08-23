# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Robot pose source: TF (map->base_link) with odometry fallback in mapfree mode.

Owns the on-demand ``/odom`` and nav-mode subscriptions (created via
:meth:`start` when the brain activates, torn down via :meth:`stop`); the
always-on TF listener keeps the transform buffer warm for on-demand lookups.
The pure ``(x, y, theta)`` math lives in :mod:`perception.pose`; this module
is the ROS-facing source of poses.
"""

from __future__ import annotations

from nav_msgs.msg import Odometry
from rclpy.time import Time
from std_msgs.msg import String
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

from brain_client.common.geometry import quaternion_to_yaw

Pose = tuple[float, float, float]


class PoseTracker:
    def __init__(self, node, *, odom_topic: str, nav_mode_topic: str):
        self._node = node
        self._logger = node.get_logger()
        self._odom_topic = odom_topic
        self._nav_mode_topic = nav_mode_topic

        self.last_odom: Odometry | None = None
        self.cur_nav_mode: str | None = None

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, node)

        self._odom_sub = None
        self._nav_mode_sub = None

    # --- on-demand lifecycle (brain active) ---
    def start(self) -> None:
        if self._odom_sub is not None:
            return
        self._odom_sub = self._node.create_subscription(Odometry, self._odom_topic, self._on_odom, 10)
        self._nav_mode_sub = self._node.create_subscription(String, self._nav_mode_topic, self._on_nav_mode, 10)

    def stop(self) -> None:
        # Destroying here is safe only because brain_client_node is spun
        # single-threaded and stop() runs on that spin thread (between
        # callbacks). On a multi-threaded executor this would race the wait
        # set (InvalidHandle) — flag-gate the callbacks instead, like
        # skills/robot_state.py.
        for sub in (self._odom_sub, self._nav_mode_sub):
            if sub is not None:
                self._node.destroy_subscription(sub)
        self._odom_sub = None
        self._nav_mode_sub = None

    @property
    def is_mapfree(self) -> bool:
        return self.cur_nav_mode == "mapfree"

    # --- callbacks ---
    def _on_odom(self, msg: Odometry) -> None:
        self.last_odom = msg

    def _on_nav_mode(self, msg: String) -> None:
        self.cur_nav_mode = msg.data
        self._logger.debug(f"Current Navigation Mode is {self.cur_nav_mode}")

    # --- pose queries ---
    def odom_pose_xyt(self) -> Pose | None:
        """Current base pose in odom, the fixed frame for mapfree goals."""
        if self.last_odom is None:
            return None
        pos = self.last_odom.pose.pose.position
        ori = self.last_odom.pose.pose.orientation
        return (pos.x, pos.y, quaternion_to_yaw(ori))

    def current_pose_xyt(self) -> Pose | None:
        """Current robot pose as (x, y, theta); None if unavailable."""
        if self.is_mapfree:
            return self.odom_pose_xyt()
        return self.map_pose_xyt()

    def map_pose_xyt(self) -> Pose | None:
        """The map->base_link pose from TF, regardless of nav mode; None if unavailable."""
        try:
            # No timeout: this runs on the agent's loop thread (twice per
            # turn), and tf2's timeout is a sleep-poll — waiting 0.5s here
            # whenever the map frame is missing stalls turns and telemetry.
            # The listener keeps the buffer warm; if the transform isn't
            # there yet, None now beats a pose half a second late.
            transform = self.tf_buffer.lookup_transform(
                target_frame="map",
                source_frame="base_link",
                time=Time(),
            )
        except Exception:
            return None
        pos = transform.transform.translation
        return (pos.x, pos.y, quaternion_to_yaw(transform.transform.rotation))
