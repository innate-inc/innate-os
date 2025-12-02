#!/usr/bin/env python3
"""
CameraInterface - Provides camera image access to primitives.

This interface allows primitives to:
1. Get the latest camera image as a decoded cv2 BGR array
2. Get the latest camera image as a base64-encoded JPEG string
"""

import base64
import cv2
import numpy as np
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy


class CameraInterface:
    """High-level interface for camera image access.

    Primitives should use this instead of directly accessing node attributes.
    Images are stored as raw CompressedImage and decoded lazily on request.
    """

    def __init__(self, node: Node, logger, image_topic: str = "/mars/main_camera/image/compressed"):
        self.node = node
        self.logger = logger
        self.image_topic = image_topic
        self._last_msg = None

        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
        )

        # Subscriber for camera images
        self._image_sub = self.node.create_subscription(
            CompressedImage,
            self.image_topic,
            self._image_callback,
            image_qos,
        )

        self.logger.info(f"CameraInterface initialized with topic: {self.image_topic}")

    def _image_callback(self, msg: CompressedImage):
        """Store raw message - no decoding here."""
        self._last_msg = msg

    def get_image(self):
        """Get the latest camera image as a decoded cv2 BGR array.

        Returns:
            numpy.ndarray or None: BGR image array, or None if no image available.
        """
        if self._last_msg is None:
            return None
        try:
            np_arr = np.frombuffer(self._last_msg.data, np.uint8)
            return cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        except Exception as e:
            self.logger.error(f"Failed to decode camera image: {e}")
            return None

    def get_image_b64(self) -> str | None:
        """Get the latest camera image as a base64-encoded JPEG string.

        Returns:
            str or None: Base64 string of JPEG data, or None if no image available.
        """
        if self._last_msg is None:
            return None
        return base64.b64encode(bytes(self._last_msg.data)).decode("utf-8")

    def has_image(self) -> bool:
        """Check if a camera image is available.

        Returns:
            bool: True if an image has been received.
        """
        return self._last_msg is not None
