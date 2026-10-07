"""Stationary feature observation and base-relative basket-face geometry."""

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import urllib.request

import numpy as np
import websocket
from innate_skills.basket_capture import live_frame
from innate_skills.basket_features import BasketDetector, Camera
from innate_skills.box_marker import camera_in_base
from innate_skills.box_nav2 import PointController, in_odom, wrap

from innate.exceptions import SkillFailed

ASSETS = Path(__file__).resolve().parents[1] / "config" / "basket_features"


def face_in_base(result, tilt):
    """Visible face centre and inward unit normal, both in base_link XY."""
    if not result.get("detected"):
        raise ValueError("Basket not visible")
    best = result.get("best", {})
    pose = best.get("pose")
    if not pose or best.get("pose_ambiguous", True):
        raise ValueError("Basket pose missing or ambiguous")
    center = np.asarray(pose.get("face_center_camera_m"), dtype=float)
    normal = np.asarray(pose.get("face_normal_camera"), dtype=float)
    rms = float(pose.get("reprojection_rms_px", math.inf))
    if (
        center.shape != (3,)
        or normal.shape != (3,)
        or not np.isfinite(center).all()
        or not np.isfinite(normal).all()
        or not math.isfinite(tilt)
        or not math.isfinite(rms)
        or not 0 <= rms <= 4
        or center[2] <= 0
        or not np.isclose(np.linalg.norm(normal), 1, atol=0.01)
    ):
        raise ValueError("Invalid basket pose")
    transform = camera_in_base(tilt)
    center = (transform @ np.r_[center, 1])[:2]
    normal = (transform[:3, :3] @ normal)[:2]
    length = np.linalg.norm(normal)
    # Same vertical-face requirement as the marker release geometry. Feature
    # normals point INTO the basket, whereas ArUco marker Z points OUT.
    if length < 0.7 or np.dot(center, normal) <= 0:
        raise ValueError("Basket face is not a usable inward-facing vertical plane")
    return center, normal / length


def release_target(center, normal, inset=0.13, right=0.08):
    return tuple(center + inset * normal + right * np.array([normal[1], -normal[0]]))


class FeatureObserver:
    """A worker can decode/infer, but never receives hardware control handles."""

    def __init__(self, assets=ASSETS, api_url=None):
        self.assets = Path(assets)
        self.api_url = (
            os.environ.get("BASKET_POSE_API_URL", "") if api_url is None else api_url
        )
        self.camera = Camera.load(self.assets / "calibration.json")
        self.calibration_id = hashlib.sha256(
            (self.assets / "calibration.json").read_bytes()
        ).hexdigest()
        self.detector = None

    def capture(self):
        not_before = time.time()
        ws = websocket.create_connection("ws://127.0.0.1:9090", timeout=1)
        try:
            for topic, kind in (
                ("/mars/main_camera/left/camera_info", "CameraInfo"),
                ("/mars/main_camera/left/image_highres/compressed", "CompressedImage"),
            ):
                ws.send(
                    json.dumps(
                        {
                            "op": "subscribe",
                            "topic": topic,
                            "type": "sensor_msgs/msg/" + kind,
                            "queue_length": 1,
                            "throttle_rate": 200,
                        }
                    )
                )
            image, meta, encoded = live_frame(ws, self.camera, not_before=not_before)
        finally:
            ws.close()
        if self.api_url:
            stamp = meta["header"]["stamp"]
            request = urllib.request.Request(
                self.api_url.rstrip("/") + "/detect",
                data=encoded,
                headers={
                    "Content-Type": "image/jpeg",
                    "X-Calibration-Id": self.calibration_id,
                    "X-Captured-At-Unix": str(stamp["sec"] + stamp["nanosec"] / 1e9),
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            if (
                result.get("image_sha256") != hashlib.sha256(encoded).hexdigest()
                or result.get("calibration_id") != self.calibration_id
            ):
                raise ValueError("API result does not match this frame/calibration")
            return result
        if self.detector is None:
            self.detector = BasketDetector(self.assets)
        return self.detector.detect(image)

    def observe(self, host, controller):
        """Three stationary attempts; cancellation never waits for inference."""
        reason = "No observation"
        for _ in range(3):
            host.mobility.stop()
            host.sleep(0.3)
            before = controller.fresh_odom()
            tilt = host.head_position.pitch_degrees
            output = queue.Queue(maxsize=1)

            def run():
                try:
                    output.put((True, self.capture()))
                except Exception as exc:
                    output.put((False, exc))

            threading.Thread(target=run, daemon=True).start()
            moved = False
            until = time.monotonic() + 45
            while output.empty():
                host.sleep(0.05)
                current = controller.fresh_odom()  # enforces overall deadline
                moved |= (
                    math.hypot(current.x - before.x, current.y - before.y) > 0.01
                    or abs(wrap(current.theta - before.theta)) > 0.02
                    or abs(host.head_position.pitch_degrees - tilt) > 1
                )
                if time.monotonic() > until:
                    raise SkillFailed(
                        "Basket observation timed out; base remains stopped"
                    )
            host.check_cancelled()
            ok, value = output.get_nowait()
            after = controller.fresh_odom()
            if (
                moved
                or math.hypot(after.x - before.x, after.y - before.y) > 0.01
                or abs(wrap(after.theta - before.theta)) > 0.02
                or abs(host.head_position.pitch_degrees - tilt) > 1
            ):
                reason = "Robot or head moved during observation"
                continue
            if not ok:
                reason = str(value)
                continue
            try:
                return face_in_base(value, tilt)
            except (ValueError, TypeError, KeyError) as exc:
                reason = str(exc)
        raise SkillFailed(
            f"No usable basket pose after three stationary attempts: {reason}"
        )


class FeatureController(PointController):
    def __init__(self, host):
        super().__init__(host)
        self.deadline = time.monotonic() + 120
        self.follower.max_linear = 0.15
        self.follower.max_angular = 0.6

    def fresh_odom(self):
        if time.monotonic() > self.deadline:
            raise SkillFailed("Feature basket alignment timed out; refusing release")
        return super().fresh_odom()


def dock_features(host, observer, verify_hold=None):
    """Stop, measure, move <=15 cm, stop and remeasure. Return a final fresh face."""
    controller = FeatureController(host)
    hold_checked = verify_hold is None
    try:
        while True:
            center, normal = observer.observe(host, controller)
            yaw = math.atan2(normal[1], normal[0])
            goal = center - host.FINAL_DISTANCE_M * normal
            if np.linalg.norm(goal) <= 0.045 and abs(yaw) <= 0.13:
                if hold_checked:
                    return center, normal
                verify_hold()
                hold_checked = True
                continue  # Fresh basket observation after a possibly slow grip check.
            hold_checked = verify_hold is None
            odom = controller.fresh_odom()
            origin = odom.x, odom.y, odom.theta
            if np.linalg.norm(goal) > 0.045:
                goal *= min(1, 0.15 / np.linalg.norm(goal))
                controller.drive(in_odom([(goal[0], goal[1], 0)], origin)[0])
                # Reface the observed basket after the bounded translation.
                basket = in_odom([(center[0], center[1], yaw)], origin)[0]
                now = controller.fresh_odom()
                controller.turn(math.atan2(basket[1] - now.y, basket[0] - now.x))
            else:
                controller.turn(wrap(odom.theta + yaw))
    finally:
        controller.close()
