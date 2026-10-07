#!/usr/bin/env python3
"""Stationary diagnostic: image file or fresh main-camera frames. No motion API."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workspace/innate_skills"))
from basket_features import BasketDetector, Camera, draw_detection  # noqa: E402 - import from the accompanying checkout/package


def live_frame(ws, camera, timeout=10):
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
        if age < -0.5 or age > 2:
            continue
        data = msg["data"]
        data = base64.b64decode(data) if isinstance(data, str) else bytes(data)
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path)
    source.add_argument("--live", action="store_true")
    p.add_argument("--frames", type=int, default=1)
    p.add_argument(
        "--assets", type=Path, default=ROOT / "workspace/config/basket_features"
    )
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--api-url",
        help="POST images to this basket API rather than run inference locally",
    )
    a = p.parse_args()
    if not 1 <= a.frames <= 100:
        p.error("frames must be between 1 and 100")
    cv2.setNumThreads(2)
    a.output.mkdir(parents=True, exist_ok=True)
    detector = None if a.api_url else BasketDetector(a.assets)
    camera = Camera.load(a.assets / "calibration.json")
    calibration_id = hashlib.sha256(
        (a.assets / "calibration.json").read_bytes()
    ).hexdigest()
    ws = None
    try:
        if a.live:
            import websocket

            ws = websocket.create_connection("ws://127.0.0.1:9090", timeout=10)
            ws.send(
                json.dumps(
                    {
                        "op": "subscribe",
                        "id": "basket-pose-calibration",
                        "topic": "/mars/main_camera/left/camera_info",
                        "type": "sensor_msgs/msg/CameraInfo",
                        "queue_length": 1,
                        "throttle_rate": 200,
                    }
                )
            )
            ws.send(
                json.dumps(
                    {
                        "op": "subscribe",
                        "id": "basket-pose-probe",
                        "topic": "/mars/main_camera/left/image_highres/compressed",
                        "type": "sensor_msgs/msg/CompressedImage",
                        "queue_length": 1,
                        "throttle_rate": 200,
                    }
                )
            )
        for i in range(a.frames if a.live else 1):
            if ws:
                image, source_meta, encoded = live_frame(ws, camera)
            else:
                image = cv2.imread(str(a.image))
                if image is None:
                    raise ValueError("Cannot decode input image")
                encoded = a.image.read_bytes()
                source_meta = {
                    "file": str(a.image),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            if a.api_url:
                import urllib.request

                headers = {
                    "Content-Type": "image/png"
                    if encoded.startswith(b"\x89PNG")
                    else "image/jpeg",
                    "X-Calibration-Id": calibration_id,
                }
                if "header" in source_meta:
                    stamp = source_meta["header"]["stamp"]
                    headers["X-Captured-At-Unix"] = str(
                        stamp["sec"] + stamp["nanosec"] / 1e9
                    )
                request = urllib.request.Request(
                    a.api_url.rstrip("/") + "/detect", data=encoded, headers=headers
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    result = json.load(response)
            else:
                result = detector.detect(image)
            result["source"] = source_meta
            result["image_size"] = [image.shape[1], image.shape[0]]
            result["coordinate_frame"] = (
                "camera optical: x right, y down, z forward; yaw of visible face normal, not unique basket heading"
            )
            prefix = (
                time.strftime("%Y%m%dT%H%M%S")
                + f"_{time.time_ns() % 1000000000:09d}_{i:03}"
            )
            (a.output / (prefix + ".json")).write_text(
                json.dumps(result, indent=2) + "\n"
            )
            suffix = ".png" if encoded.startswith(b"\x89PNG") else ".jpg"
            (a.output / (prefix + "_input" + suffix)).write_bytes(encoded)
            cv2.imwrite(
                str(a.output / (prefix + "_overlay.jpg")),
                draw_detection(image, result, camera),
            )
            best = result.get("best", {})
            print(
                json.dumps(
                    {
                        "result": prefix,
                        "detected": result["detected"],
                        "ms": result["processing_ms"],
                        "reference": best.get("reference"),
                        "inliers": best.get("inliers"),
                        "ambiguous": best.get("pose_ambiguous"),
                        "pose": best.get("pose"),
                    }
                ),
                flush=True,
            )
    finally:
        if ws:
            try:
                ws.send(
                    json.dumps(
                        {
                            "op": "unsubscribe",
                            "id": "basket-pose-probe",
                            "topic": "/mars/main_camera/left/image_highres/compressed",
                        }
                    )
                )
            finally:
                ws.close()


if __name__ == "__main__":
    main()
