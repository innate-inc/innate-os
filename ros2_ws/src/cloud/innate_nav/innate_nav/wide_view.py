#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Publishes the policy's virtual camera: main_camera/left, undistorted and
re-projected to the 110-degree pinhole the checkpoint was trained on.

Not image_rect. That stream is stereo-rectified with alpha=0, which crops to
pixels valid in BOTH cameras -- right for depth, but it spends about 20 degrees
of field doing it, leaving ~100 where the policy needs 110. This undistorts the
raw image itself, monocular, with R = identity, and leaves the depth pipeline
untouched.

The lens comes from camera_info when the robot has a calibration and from
nominal parameters when it does not: every unit ships the same camera, so the
nominal is close, and per-unit spread costs a fraction of a degree at the
edges. A calibrated robot is still better, and is used automatically.

The OUTPUT is fixed by the checkpoint and identical on every robot -- the
policy reads metric distance out of pixel positions and cannot absorb per-unit
variation. Anything the lens cannot cover is published BLANK and logged as an
error, never stretched to fit.
"""

from __future__ import annotations

import time

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, CompressedImage

from .wide_view_maps import Lens, Pinhole, undistort_maps

SENSOR_QOS = QoSProfile(
    reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, depth=1
)

# Measured on a MARS left camera at 640x480 (fx != fy because the driver
# squashes 1280x720 into 4:3). Used only when the robot has no calibration.
NOMINAL_K = [198.0389, 0.0, 326.8270, 0.0, 264.6403, 229.3660, 0.0, 0.0, 1.0]
NOMINAL_D = [0.0128943, -0.0305921, -0.000264048, -0.000176999, 0.00519322]
NOMINAL_SIZE = (640, 480)


class WideView(Node):
    def __init__(self) -> None:
        super().__init__("wide_view")

        self.declare_parameter("source_topic", "/mars/main_camera/left/image_raw/compressed")
        self.declare_parameter("source_info_topic", "/mars/main_camera/left/camera_info")
        self.declare_parameter("output_topic", "/mars/main_camera/left/wide/image_rect/compressed")
        self.declare_parameter("hfov_deg", 110.0)
        self.declare_parameter("width", 640)
        # 370, not the 480 training rendered: a 4:3 frame at 110 deg wants 93.9
        # degrees vertically and this sensor has ~84. The rows dropped are the
        # nearest ~7cm of floor, at the robot's feet. The training crop must
        # match whatever this is.
        self.declare_parameter("height", 370)
        self.declare_parameter("jpeg_quality", 80)
        self.declare_parameter("use_camera_info", True)

        self._dst = Pinhole.from_hfov(
            float(self.get_parameter("hfov_deg").value),
            int(self.get_parameter("width").value),
            int(self.get_parameter("height").value),
        )
        self._quality = int(self.get_parameter("jpeg_quality").value)
        self._info: Lens | None = None
        self._maps: tuple[np.ndarray, np.ndarray] | None = None
        self._built_for: tuple[int, int] | None = None
        self._warned_nominal = False
        self._sub = None
        self._started = time.monotonic()

        self._pub = self.create_publisher(
            CompressedImage, str(self.get_parameter("output_topic").value), SENSOR_QOS
        )
        self._info_pub = self.create_publisher(
            CameraInfo,
            str(self.get_parameter("output_topic").value).rsplit("/", 2)[0] + "/camera_info",
            10,
        )
        if bool(self.get_parameter("use_camera_info").value):
            self.create_subscription(
                CameraInfo, str(self.get_parameter("source_info_topic").value), self._on_info, 10
            )
        self.create_timer(1.0, self._follow_demand)
        self.get_logger().info(
            f"wide view {self._dst.width}x{self._dst.height} @ "
            f"{float(self.get_parameter('hfov_deg').value):.1f} deg, fx={self._dst.fx:.2f}"
        )

    def _on_info(self, msg: CameraInfo) -> None:
        """K and D describe the raw lens; P and R belong to the stereo pipeline
        and are deliberately ignored. K[0]=0 is how the driver reports a unit
        with no calibration on disk."""
        if self._info is not None or not msg.k[0]:
            return
        self._info = Lens.from_camera_info(msg.k, msg.d, msg.width, msg.height)
        self._built_for = None
        self.get_logger().info(
            f"using this robot's calibration: {msg.distortion_model}, {len(msg.d)} coeffs, "
            f"fx={msg.k[0]:.1f} fy={msg.k[4]:.1f} at {msg.width}x{msg.height}"
        )

    def _lens_for(self, width: int, height: int) -> Lens:
        if self._info is not None:
            return self._info.scaled_to(width, height)
        return Lens.from_camera_info(NOMINAL_K, NOMINAL_D, *NOMINAL_SIZE).scaled_to(width, height)

    def _build(self, width: int, height: int) -> None:
        lens = self._lens_for(width, height)
        map_x, map_y, valid = undistort_maps(lens, self._dst)
        self._maps = (map_x, map_y)
        self._built_for = (width, height)
        covered = float(valid.mean())
        h, v = lens.half_angles_deg()
        need_h, need_v = self._dst.half_angles_deg()
        source = "camera_info" if self._info is not None else "NOMINAL, uncalibrated robot"
        if covered < 0.999:
            self.get_logger().error(
                f"lens covers {100 * covered:.1f}% of the requested view -- the rest is published "
                f"BLANK. From {width}x{height} [{source}] it reaches {h:.1f}/{v:.1f} deg "
                f"(horizontal/vertical) and the view needs {need_h:.1f}/{need_v:.1f}. "
                f"Lower hfov_deg or height until this reads 100%."
            )
        else:
            self.get_logger().info(
                f"full coverage from {width}x{height} [{source}]: lens reaches {h:.1f}/{v:.1f} deg, "
                f"view needs {need_h:.1f}/{need_v:.1f}"
            )

    def _follow_demand(self) -> None:
        """Subscribe only while something is listening: the decode and the
        remap are the cost, and an idle robot should pay neither."""
        if (self._info is None and not self._warned_nominal
                and bool(self.get_parameter("use_camera_info").value)
                and time.monotonic() - self._started > 15.0):
            self._warned_nominal = True
            self.get_logger().warning(
                f"no calibration on {self.get_parameter('source_info_topic').value} after 15s; "
                f"using nominal lens parameters for this camera model"
            )
        wanted = self._pub.get_subscription_count() > 0
        if wanted and self._sub is None:
            self._sub = self.create_subscription(
                CompressedImage, str(self.get_parameter("source_topic").value),
                self._on_frame, SENSOR_QOS,
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
        # Built against the frame's own size, not camera_info's: the driver can
        # publish at a resolution it was not calibrated at.
        size = (src.shape[1], src.shape[0])
        if self._built_for != size:
            self._build(*size)
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
