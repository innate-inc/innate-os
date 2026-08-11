#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Publishes the nav policy's camera view on hardware, by remapping the head
camera's fisheye into the virtual pinhole the policy was trained on.

In sim the world server renders that view natively, so this node is the
hardware half of the same contract: same topic, same intrinsics, different
producer. The policy client cannot tell which one it is talking to.

The remap is a fixed table (innate_nav_inference.client.rectify), built once
from the two camera models and applied per frame. Idle unless something
subscribes -- the input subscription is dropped when the output has no
listeners, so a robot that is not navigating pays nothing.

The source lens parameters MUST come from a real fisheye calibration.
mars_cam's stereo calibration is plumb_bob, which does not fit a lens this
wide; the defaults here are innate-nav-data's `mars` front_fish preset and are
a starting point, not a measurement.
"""

from __future__ import annotations

import cv2
import numpy as np
import rclpy
from innate_nav_inference.client.rectify import DoubleSphere, Pinhole, build_maps, coverage
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, CompressedImage

SENSOR_QOS = QoSProfile(
    reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, depth=1
)


class NavCameraRectifier(Node):
    def __init__(self) -> None:
        super().__init__("nav_camera_rectifier")

        self.declare_parameter("source_topic", "/mars/main_camera/left/image_raw/compressed")
        self.declare_parameter("output_topic", "/mars/nav_camera/image_raw/compressed")
        self.declare_parameter("hfov_deg", 110.0)
        self.declare_parameter("width", 640)
        self.declare_parameter("height", 480)
        self.declare_parameter("jpeg_quality", 80)
        # Source lens (double sphere). fx/fy/cx/cy default to the source frame's
        # own geometry only so the node starts; they are not a calibration.
        self.declare_parameter("source_fx", 0.0)
        self.declare_parameter("source_fy", 0.0)
        self.declare_parameter("source_cx", 0.0)
        self.declare_parameter("source_cy", 0.0)
        self.declare_parameter("source_xi", -0.27)
        self.declare_parameter("source_alpha", 0.57)

        w = int(self.get_parameter("width").value)
        h = int(self.get_parameter("height").value)
        self._dst = Pinhole.from_hfov(float(self.get_parameter("hfov_deg").value), w, h)
        self._quality = int(self.get_parameter("jpeg_quality").value)
        self._maps: tuple[np.ndarray, np.ndarray] | None = None
        self._sub = None

        self._pub = self.create_publisher(
            CompressedImage, str(self.get_parameter("output_topic").value), SENSOR_QOS
        )
        self._info_pub = self.create_publisher(
            CameraInfo, str(self.get_parameter("output_topic").value).rsplit("/", 2)[0] + "/camera_info", 10
        )
        self.create_timer(1.0, self._follow_demand)
        self.get_logger().info(
            f"nav rectifier ready: {self._dst.width}x{self._dst.height} @ "
            f"{float(self.get_parameter('hfov_deg').value):.1f} deg, fx={self._dst.fx:.2f}"
        )

    def _lens(self, src_w: int, src_h: int) -> DoubleSphere:
        def p(name: str, fallback: float) -> float:
            v = float(self.get_parameter(name).value)
            return v if v > 0.0 else fallback

        return DoubleSphere(
            fx=p("source_fx", src_w / 2),
            fy=p("source_fy", src_w / 2),
            cx=p("source_cx", src_w / 2),
            cy=p("source_cy", src_h / 2),
            xi=float(self.get_parameter("source_xi").value),
            alpha=float(self.get_parameter("source_alpha").value),
            width=src_w,
            height=src_h,
        )

    def _follow_demand(self) -> None:
        """Subscribe only while someone is listening: the decode and the remap
        are the cost, and an idle robot should pay neither."""
        wanted = self._pub.get_subscription_count() > 0
        if wanted and self._sub is None:
            self._sub = self.create_subscription(
                CompressedImage, str(self.get_parameter("source_topic").value), self._on_frame, SENSOR_QOS
            )
            self.get_logger().info("subscriber appeared; rectifying")
        elif not wanted and self._sub is not None:
            self.destroy_subscription(self._sub)
            self._sub = None
            self.get_logger().info("no subscribers; idle")

    def _on_frame(self, msg: CompressedImage) -> None:
        src = cv2.imdecode(np.frombuffer(msg.data, np.uint8), cv2.IMREAD_COLOR)
        if src is None:
            return
        if self._maps is None:
            self._build(src.shape[1], src.shape[0])
        map_x, map_y = self._maps
        out = cv2.remap(src, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

        rect = CompressedImage()
        rect.header = msg.header
        rect.format = "jpeg"
        rect.data = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, self._quality])[1].tobytes()
        self._pub.publish(rect)

        info = CameraInfo()
        info.header = msg.header
        info.width, info.height = self._dst.width, self._dst.height
        info.distortion_model = "plumb_bob"
        info.d = [0.0] * 5
        info.k = self._dst.k
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        info.p = [self._dst.fx, 0.0, self._dst.cx, 0.0, 0.0, self._dst.fy, self._dst.cy, 0.0, 0.0, 0.0, 1.0, 0.0]
        self._info_pub.publish(info)

    def _build(self, src_w: int, src_h: int) -> None:
        lens = self._lens(src_w, src_h)
        map_x, map_y, valid = build_maps(lens, self._dst)
        self._maps = (map_x, map_y)
        reach = float(valid.mean())
        if reach < 0.999:
            # Blank wedges are worse than a narrower view: the policy has never
            # seen a black corner, and it cannot tell one from a dark obstacle.
            self.get_logger().error(
                f"lens reaches only {100 * reach:.0f}% of a {self._dst.width}x{self._dst.height} "
                f"@ {2 * np.degrees(np.arctan(self._dst.width / 2 / self._dst.fx)):.0f} deg view "
                f"(source fx={lens.fx:.0f}) -- the rest is published BLANK. Calibrate the lens, "
                f"or lower hfov_deg until coverage is 100%."
            )
        else:
            self.get_logger().info(f"remap built from {src_w}x{src_h}, full coverage")


def main(args=None) -> None:
    rclpy.init(args=args)
    node = NavCameraRectifier()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
