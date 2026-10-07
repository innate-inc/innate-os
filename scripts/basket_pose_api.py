#!/usr/bin/env python3
"""Portable JPEG/PNG -> basket face pose HTTP API; no ROS or motion dependency.

One inference at a time: the detector's OpenCV matcher is mutable. Put the
loopback service behind your authenticated reverse proxy, or use --token-file
for direct access on a trusted LAN.
"""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workspace/innate_skills"))
from basket_features import BasketDetector  # noqa: E402 - import from the accompanying checkout/package

MAX_BYTES = 8 * 1024 * 1024
# These are the robot driver's known, full-FOV outputs. Crops must never silently
# reuse the same intrinsics. Add other full-FOV sizes deliberately if needed.
IMAGE_SIZES = {(1280, 720), (640, 480)}


def make_server(assets, host="127.0.0.1", port=9071, auth_token=None):
    if auth_token is not None and not auth_token.strip():
        raise ValueError("API authentication token must not be empty")
    assets = Path(assets)
    detector = BasketDetector(assets)
    calibration_id = hashlib.sha256(
        (assets / "calibration.json").read_bytes()
    ).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def send_json(self, status, body):
            data = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def authorized(self):
            if auth_token is not None and not secrets.compare_digest(
                self.headers.get("Authorization", "").encode(),
                ("Bearer " + auth_token).encode(),
            ):
                self.send_json(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/health":
                self.send_json(404, {"error": "not_found"})
                return
            self.send_json(
                200,
                {
                    "status": "ready",
                    "references": len(detector.references),
                    "algorithm": detector.algorithm,
                    "calibration_id": calibration_id,
                    "accepted_image_sizes": sorted(IMAGE_SIZES),
                    "metric_accuracy_verified": False,
                },
            )

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/detect":
                self.send_json(404, {"error": "not_found"})
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_json(400, {"error": "chunked_transfer_not_supported"})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() not in (
                "image/jpeg",
                "image/png",
            ):
                self.send_json(415, {"error": "expected_image_jpeg_or_png"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                self.send_json(411, {"error": "content_length_required"})
                return
            try:
                if len(lengths) != 1:
                    raise ValueError("Multiple content lengths")
                length = int(lengths[0])
            except ValueError:
                self.send_json(400, {"error": "invalid_content_length"})
                return
            if not 0 < length <= MAX_BYTES:
                self.send_json(
                    413 if length > MAX_BYTES else 400,
                    {"error": "invalid_image_size_bytes"},
                )
                return
            expected_cal = self.headers.get("X-Calibration-Id")
            if expected_cal and expected_cal != calibration_id:
                self.send_json(
                    409,
                    {
                        "error": "camera_calibration_mismatch",
                        "calibration_id": calibration_id,
                    },
                )
                return
            captured = self.headers.get("X-Captured-At-Unix")
            age = None
            if captured is not None:
                try:
                    age = time.time() - float(captured)
                    if not np.isfinite(age) or age < -1 or age > 3:
                        raise ValueError("Stale or invalid timestamp")
                except ValueError:
                    self.send_json(422, {"error": "stale_or_invalid_capture_timestamp"})
                    return
            started = time.perf_counter()
            try:
                data = self.rfile.read(length)
                if len(data) != length:
                    self.send_json(400, {"error": "incomplete_image_body"})
                    return
                image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    self.send_json(400, {"error": "undecodable_image"})
                    return
                size = (image.shape[1], image.shape[0])
                if size not in IMAGE_SIZES:
                    self.send_json(
                        422,
                        {
                            "error": "unsupported_image_dimensions",
                            "accepted_image_sizes": sorted(IMAGE_SIZES),
                        },
                    )
                    return
                result = detector.detect(image)
                result.update(
                    image_size=list(size),
                    calibration_id=calibration_id,
                    image_sha256=hashlib.sha256(data).hexdigest(),
                    request_id=self.headers.get("X-Request-Id", "")[:128],
                    capture_age_at_request_s=age,
                    response_latency_ms=(time.perf_counter() - started) * 1000,
                    coordinate_frame="camera optical: x right, y down, z forward",
                    orientation_semantics="visible face normal; not a unique 360-degree basket heading",
                    usage="diagnostic; unverified physical accuracy; not a robot motion command",
                )
                self.send_json(200, result)
            except (TimeoutError, ConnectionError, BrokenPipeError):
                self.close_connection = True
            except (ValueError, cv2.error):
                self.send_json(422, {"error": "image_processing_failed"})

        def log_message(self, fmt, *args):
            # Avoid logging payloads, camera images or user-provided text.
            return

    return HTTPServer((host, port), Handler)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--assets", type=Path, default=ROOT / "workspace/config/basket_features"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9071)
    p.add_argument(
        "--threads",
        type=int,
        default=2,
        help="OpenCV worker threads; inference requests remain serial",
    )
    p.add_argument(
        "--token-file", type=Path, help="Shared bearer token file for direct LAN access"
    )
    a = p.parse_args()
    if not 1 <= a.threads <= 64:
        p.error("threads must be 1..64")
    cv2.setNumThreads(a.threads)
    token = a.token_file.read_text().strip() if a.token_file else None
    server = make_server(a.assets, a.host, a.port, auth_token=token)
    print(f"Basket pose API ready on http://{a.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
