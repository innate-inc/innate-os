"""Fresh, calibration-checked main-camera acquisition shared by skills and probes."""

import base64
import hashlib
import json
import time

import cv2
import numpy as np


def live_frame(ws, camera, timeout=10, not_before=0):
    calibration_checked = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = json.loads(ws.recv())
        except TimeoutError:
            continue
        if (
            msg.get("op") == "publish"
            and msg.get("topic") == "/mars/main_camera/left/camera_info"
        ):
            info = msg["msg"]
            expected = camera.scaled((info["width"], info["height"]))
            if (
                info.get("distortion_model") != "plumb_bob"
                or not np.allclose(
                    np.asarray(info["k"]).reshape(3, 3), expected, atol=1e-6, rtol=0
                )
                or not np.allclose(info["d"], camera.distortion, atol=1e-8, rtol=0)
            ):
                raise ValueError(
                    "Live camera calibration changed; update the basket model calibration"
                )
            calibration_checked = True
            continue
        if (
            msg.get("op") != "publish"
            or msg.get("topic") != "/mars/main_camera/left/image_highres/compressed"
        ):
            continue
        if not calibration_checked:
            continue
        msg = msg["msg"]
        stamp = msg.get("header", {}).get("stamp", {})
        age = time.time() - (stamp.get("sec", 0) + stamp.get("nanosec", 0) / 1e9)
        captured_at = stamp.get("sec", 0) + stamp.get("nanosec", 0) / 1e9
        if age < -0.5 or age > 2 or captured_at < not_before:
            continue
        data = msg["data"]
        data = base64.b64decode(data) if isinstance(data, str) else bytes(data)
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None or (image.shape[1], image.shape[0]) not in (
            (1280, 720),
            (640, 480),
        ):
            continue
        return (
            image,
            {
                "header": msg["header"],
                "age_at_receive_s": age,
                "jpeg_sha256": hashlib.sha256(data).hexdigest(),
            },
            data,
        )
    raise TimeoutError("No fresh camera frame received")
