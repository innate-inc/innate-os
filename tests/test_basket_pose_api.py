"""Exercise the real HTTP decoder -> detector -> JSON path on an ephemeral port."""

import base64
import http.client
import json
from pathlib import Path
import sys
import threading
import time
import unittest

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from basket_pose_api import make_server, MAX_BYTES  # noqa: E402 - import from the accompanying checkout/package


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cv2.setNumThreads(2)
        cls.server = make_server(ROOT / "workspace/config/basket_features", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.blank = cv2.imencode(".jpg", np.zeros((720, 1280, 3), np.uint8))[
            1
        ].tobytes()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path="/detect", body=None, headers=None, method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        try:
            h = {"Content-Type": "image/jpeg", **(headers or {})}
            c.request(method, path, self.blank if body is None else body, h)
            response = c.getresponse()
            return response.status, json.loads(response.read())
        finally:
            c.close()

    def test_health_and_real_positive_request(self):
        status, health = self.request("/health", method="GET")
        self.assertEqual(status, 200)
        self.assertFalse(health["metric_accuracy_verified"])
        crop = cv2.imread(str(ROOT / "workspace/config/basket_features/close-long.png"))
        scene = np.zeros((720, 1280, 3), np.uint8)
        h, w = crop.shape[:2]
        scene[430 : 430 + h, 440 : 440 + w] = crop
        jpg = cv2.imencode(".jpg", scene)[1].tobytes()
        status, result = self.request(
            body=jpg,
            headers={
                "X-Calibration-Id": health["calibration_id"],
                "X-Request-Id": "test-positive",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["detected"])
        self.assertEqual(result["request_id"], "test-positive")
        self.assertIn("pose", result["best"])
        self.assertEqual(
            result["coordinate_frame"], "camera optical: x right, y down, z forward"
        )
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(result["detected"])
        self.assertNotIn("best", result)

    def test_bad_payloads_and_dimensions(self):
        for body, headers, expected in [
            (b"bad jpeg", {}, 400),
            (b"", {}, 400),
            (b"{}", {"Content-Type": "application/json"}, 415),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(self.request(body=body, headers=headers)[0], expected)
        small = cv2.imencode(".jpg", np.zeros((100, 100, 3), np.uint8))[1].tobytes()
        self.assertEqual(self.request(body=small)[0], 422)
        self.assertEqual(
            self.request(headers={"Content-Length": str(MAX_BYTES + 1)})[0], 413
        )
        self.assertEqual(self.request(headers={"Content-Length": "abc"})[0], 400)

    def test_stale_frames_and_wrong_calibration(self):
        for stamp in ("nan", str(time.time() - 60), str(time.time() + 60)):
            self.assertEqual(
                self.request(headers={"X-Captured-At-Unix": stamp})[0], 422
            )
        self.assertEqual(
            self.request(headers={"X-Calibration-Id": "wrong-camera"})[0], 409
        )
        self.assertEqual(
            self.request(headers={"X-Captured-At-Unix": str(time.time())})[0], 200
        )

    def test_missing_route(self):
        self.assertEqual(self.request("/motion")[0], 404)


class LiveCaptureTests(unittest.TestCase):
    def test_camera_calibration_and_frame_freshness(self):
        from basket_pose_probe import live_frame, Camera

        camera = Camera.load(ROOT / "workspace/config/basket_features/calibration.json")
        encoded = cv2.imencode(".jpg", np.zeros((720, 1280, 3), np.uint8))[1].tobytes()
        info = {
            "width": camera.image_size[0],
            "height": camera.image_size[1],
            "k": camera.matrix.ravel().tolist(),
            "d": camera.distortion.tolist(),
            "distortion_model": "plumb_bob",
        }

        def packet(topic, msg):
            return json.dumps({"op": "publish", "topic": topic, "msg": msg})

        def frame(stamp):
            return packet(
                "/mars/main_camera/left/image_highres/compressed",
                {
                    "header": {
                        "stamp": {"sec": int(stamp), "nanosec": int((stamp % 1) * 1e9)}
                    },
                    "data": base64.b64encode(encoded).decode(),
                },
            )

        class FakeSocket:
            def __init__(self, messages):
                self.messages = iter(messages)

            def recv(self):
                return next(self.messages)

        socket = FakeSocket(
            [
                packet("/mars/main_camera/left/camera_info", info),
                frame(time.time() - 60),
                frame(time.time()),
            ]
        )
        image, meta, original = live_frame(socket, camera)
        self.assertEqual(image.shape, (720, 1280, 3))
        self.assertLess(meta["age_at_receive_s"], 2)
        self.assertEqual(original, encoded)
        info["k"][0] += 2
        with self.assertRaisesRegex(ValueError, "calibration changed"):
            live_frame(
                FakeSocket([packet("/mars/main_camera/left/camera_info", info)]), camera
            )


if __name__ == "__main__":
    unittest.main()
