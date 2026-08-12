#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Publishes the policy's virtual camera by resampling the already-rectified
left image.

There is no lens model here on purpose. mars_cam's stereo pipeline already
undistorts main_camera/left with THAT ROBOT's calibration (a 14-coefficient
rational fit) and publishes the result as image_rect_color. Both that image and
the policy's view are pinholes sharing an optical axis, so going from one to
the other is a crop and a scale -- no distortion coefficients, and nothing here
to calibrate.

The source geometry is read from the rectified CameraInfo (P, not K -- P is
what image_rect was projected with), so a unit whose calibration differs
slightly is handled without configuration. The OUTPUT is fixed by the
checkpoint, identical on every robot: that is the point, since the policy reads
metric distance out of pixels and cannot absorb per-unit variation.

A view the camera cannot cover is published BLANK in the uncovered region and
logged as an error, rather than stretched to fit -- the policy has never seen a
black band, and it has never seen a rescaled one either.
"""

from __future__ import annotations

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, CompressedImage

from .wide_view_maps import Pinhole, resample_maps

SENSOR_QOS = QoSProfile(
    reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, depth=1
)


class WideView(Node):
    def __init__(self) -> None:
        super().__init__("wide_view")

        self.declare_parameter("source_topic", "/mars/main_camera/left/image_rect_color/compressed")
        self.declare_parameter("source_info_topic", "/mars/main_camera/left/camera_info")
        self.declare_parameter("output_topic", "/mars/main_camera/left/wide/image_rect/compressed")
        self.declare_parameter("hfov_deg", 110.0)
        self.declare_parameter("width", 640)
        # 370, not the 480 training rendered: this sensor is 16:9, so a 4:3
        # frame at 110 deg would need 93.9 deg vertically and it has ~81. The
        # rows dropped are the nearest ~7cm of floor, at the robot's feet.
        # 370 rather than the 380 that just fits, so ~10px of per-unit
        # principal-point spread cannot push a robot below full coverage.
        self.declare_parameter("height", 370)
        self.declare_parameter("jpeg_quality", 80)

        self._dst = Pinhole.from_hfov(
            float(self.get_parameter("hfov_deg").value),
            int(self.get_parameter("width").value),
            int(self.get_parameter("height").value),
        )
        self._quality = int(self.get_parameter("jpeg_quality").value)
        self._maps: tuple[np.ndarray, np.ndarray] | None = None
        self._src: Pinhole | None = None
        self._sub = None

        self._pub = self.create_publisher(
            CompressedImage, str(self.get_parameter("output_topic").value), SENSOR_QOS
        )
        self._info_pub = self.create_publisher(
            CameraInfo,
            str(self.get_parameter("output_topic").value).rsplit("/", 2)[0] + "/camera_info",
            10,
        )
        self.create_subscription(
            CameraInfo, str(self.get_parameter("source_info_topic").value), self._on_info, 10
        )
        self.create_timer(1.0, self._follow_demand)
        self.get_logger().info(
            f"wide view {self._dst.width}x{self._dst.height} @ "
            f"{float(self.get_parameter('hfov_deg').value):.1f} deg, fx={self._dst.fx:.2f}; "
            f"waiting for {self.get_parameter('source_info_topic').value}"
        )

    def _on_info(self, msg: CameraInfo) -> None:
        """P carries the rectified projection; K still describes the raw lens."""
        if self._src is not None:
            return
        self._src = Pinhole(fx=msg.p[0], fy=msg.p[5], cx=msg.p[2], cy=msg.p[6],
                            width=msg.width, height=msg.height)
        map_x, map_y, valid = resample_maps(self._src, self._dst)
        self._maps = (map_x, map_y)
        covered = float(valid.mean())
        if covered < 0.999:
            self.get_logger().error(
                f"this camera covers {100 * covered:.1f}% of the requested view "
                f"(source fx={self._src.fx:.1f} {self._src.width}x{self._src.height}); "
                f"the rest is published BLANK. Lower hfov_deg or height until it reaches 100%."
            )
        else:
            self.get_logger().info(
                f"resampling from {self._src.width}x{self._src.height} fx={self._src.fx:.1f}, "
                f"full coverage"
            )

    def _follow_demand(self) -> None:
        """Subscribe only while something is listening: the decode and the
        resample are the cost, and an idle robot should pay neither."""
        wanted = self._pub.get_subscription_count() > 0 and self._maps is not None
        if wanted and self._sub is None:
            self._sub = self.create_subscription(
                CompressedImage, str(self.get_parameter("source_topic").value),
                self._on_frame, SENSOR_QOS,
            )
            self.get_logger().info("subscriber appeared; resampling")
        elif not wanted and self._sub is not None:
            self.destroy_subscription(self._sub)
            self._sub = None
            self.get_logger().info("no subscribers; idle")

    def _on_frame(self, msg: CompressedImage) -> None:
        src = cv2.imdecode(np.frombuffer(msg.data, np.uint8), cv2.IMREAD_COLOR)
        if src is None or self._maps is None:
            return
        map_x, map_y = self._maps
        out = cv2.remap(src, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

        frame = CompressedImage()
        frame.header = msg.header
        frame.format = "jpeg"
        frame.data = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, self._quality])[1].tobytes()
        self._pub.publish(frame)

        info = CameraInfo()
        info.header = msg.header
        info.width, info.height = self._dst.width, self._dst.height
        info.distortion_model = "plumb_bob"
        info.d = [0.0] * 5
        info.k = self._dst.k
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        info.p = [self._dst.fx, 0.0, self._dst.cx, 0.0,
                  0.0, self._dst.fy, self._dst.cy, 0.0,
                  0.0, 0.0, 1.0, 0.0]
        self._info_pub.publish(info)


def main(args=None) -> None:
    rclpy.init(args=args)
    node = WideView()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
