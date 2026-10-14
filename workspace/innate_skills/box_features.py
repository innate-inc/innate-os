"""Stationary feature observation and base-relative basket-face geometry."""

import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
from urllib.parse import urlsplit

import numpy as np
import cv2
from innate_skills.basket_features import BasketDetector, Camera
from innate_skills.demo_config import load_demo_config
from innate_skills.box_marker import camera_in_base, marker_image
from innate_skills.box_nav2 import PointController, in_odom, wrap

from innate.exceptions import SkillCancelled, SkillFailed

ASSETS = Path(__file__).resolve().parents[1] / "config" / "basket_features"


def face_in_base(result, tilt):
    """Visible face centre and inward unit normal, both in base_link XY."""
    if not result.get("detected"):
        raise ValueError("Basket not visible")
    candidates = [result.get("best", {})] + result.get("candidates", [])
    reason = "No candidate pose"
    for best in candidates:
        try:
            return candidate_face_in_base(best, tilt)
        except (ValueError, TypeError, KeyError) as exc:
            reason = str(exc)
    raise ValueError(reason)


def candidate_face_in_base(best, tilt):
    """Apply the same pose checks to every independently matched face."""
    pose = best.get("pose")
    if not pose or best.get("pose_ambiguous", True):
        if best.get("pose_ambiguous", True):
            raise ValueError("Basket pose ambiguous")
        errors = [
            p.get("reprojection_rms_px") for p in best.get("pose_alternatives", [])
        ]
        raise ValueError(
            f"Basket recognized but metric pose rejected (candidate RMS px: {errors})"
        )
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


class BasketMissing(SkillFailed):
    """A fresh calibrated frame contains no supported basket match."""


class FeatureObserver:
    """A worker can decode/infer, but never receives hardware control handles."""

    def __init__(self, assets=ASSETS, api_url=None, api_token=None):
        self.assets = Path(assets)
        runtime = self.assets / "runtime.json"
        config = load_demo_config(runtime) if api_url is None else {}
        self.api_url = (
            os.environ.get("BASKET_POSE_API_URL", config.get("api_url", ""))
            if api_url is None
            else api_url
        )
        token_file = os.environ.get(
            "BASKET_POSE_API_TOKEN_FILE", config.get("token_file", "")
        )
        self.api_token = api_token
        if self.api_url and api_token is None and token_file:
            self.api_token = Path(token_file).expanduser().read_text().strip()
            if not self.api_token:
                raise ValueError("Basket API token file is empty")
        self.camera = Camera.load(self.assets / "calibration.json")
        self.calibration_id = hashlib.sha256(
            (self.assets / "calibration.json").read_bytes()
        ).hexdigest()
        self.detector = None
        self._connection = None
        self._closed = False
        self._request_lock = threading.Lock()

    def close(self):
        """Release idle connections without waiting for a cancelled inference."""
        self._closed = True
        if self._request_lock.acquire(blocking=False):
            try:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
            finally:
                self._request_lock.release()

    @staticmethod
    def fresh_frame(host, controller, tick=None):
        """Read the same injected stream as ArUco, after settling.

        Image identity changes once per received frame. Do not reconnect to
        rosbridge or manufacture a sensor capture timestamp from local time.
        """
        previous = marker_image(host)
        until = time.monotonic() + 3
        while True:
            host.check_cancelled()
            controller.fresh_odom()  # preserve the phase deadline
            if tick is not None:
                tick()
            frame = marker_image(host)
            if frame is not None and frame is not previous:
                return frame.jpeg
            if time.monotonic() >= until:
                raise SkillFailed(
                    "Camera stream stopped updating; base remains stopped"
                )
            host.sleep(0.02)

    def capture(self, encoded):
        """Infer from an immutable JPEG selected on the skill thread."""
        if self.api_url:
            with self._request_lock:
                if self._closed:
                    raise ValueError("Basket observer is closed")
                if self._connection is None:
                    url = urlsplit(self.api_url)
                    if url.scheme not in ("http", "https") or not url.hostname:
                        raise ValueError("Basket API URL must be HTTP or HTTPS")
                    connection_type = (
                        http.client.HTTPSConnection
                        if url.scheme == "https"
                        else http.client.HTTPConnection
                    )
                    self._connection = connection_type(
                        url.hostname, url.port, timeout=30
                    )
                    self._detect_path = url.path.rstrip("/") + "/detect"
                try:
                    self._connection.request(
                        "POST",
                        self._detect_path,
                        body=encoded,
                        headers={
                            "Content-Type": "image/jpeg",
                            **(
                                {"Authorization": "Bearer " + self.api_token}
                                if self.api_token
                                else {}
                            ),
                            "X-Calibration-Id": self.calibration_id,
                        },
                    )
                    response = self._connection.getresponse()
                    data = response.read()  # drain before reusing the connection
                    if response.status != 200:
                        raise ValueError(f"Basket API returned HTTP {response.status}")
                    result = json.loads(data)
                except Exception:
                    self._connection.close()
                    self._connection = None
                    # Let observe retry on a NEW frame, not the old JPEG.
                    raise
                finally:
                    if self._closed and self._connection is not None:
                        self._connection.close()
                        self._connection = None
            if (
                result.get("image_sha256") != hashlib.sha256(encoded).hexdigest()
                or result.get("calibration_id") != self.calibration_id
            ):
                raise ValueError("API result does not match this frame/calibration")
            return result
        if self.detector is None:
            self.detector = BasketDetector(self.assets)
        image = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Invalid camera JPEG")
        return self.detector.detect(image)

    def recognize(self, host, controller, tick):
        """Detect during a search turn; never use a moving frame as a pose."""
        encoded = self.fresh_frame(host, controller, tick=tick)
        output = queue.Queue(maxsize=1)

        def run():
            try:
                output.put((True, self.capture(encoded)))
            except Exception as exc:
                output.put((False, exc))

        started = time.monotonic()
        threading.Thread(target=run, daemon=True).start()
        while output.empty():
            host.check_cancelled()
            tick()
            host.sleep(0.05)
            if time.monotonic() - started > 45:
                raise TimeoutError("Basket observation timed out")
        host.check_cancelled()
        ok, result = output.get_nowait()
        if not ok:
            raise result
        host.logger.info(
            f"[FeatureSearch] detected={result.get('detected')} "
            f"features={result.get('query_features')} "
            f"candidates={len(result.get('candidates', []))} "
            f"api_s={time.monotonic() - started:.3f}"
        )
        return result.get("detected") is True

    def observe(self, host, controller, *, search=False):
        """Retry pose fitting within the phase deadline; sensor errors get three tries."""
        reason = "No observation"
        attempts = sensor_failures = 0
        while search or attempts < 3:
            attempts += 1
            host.mobility.stop()
            host.sleep(0.3)
            before = controller.fresh_odom()
            tilt = host.head_position.pitch_degrees
            encoded = self.fresh_frame(host, controller)
            output = queue.Queue(maxsize=1)

            def run(encoded=encoded, output=output):
                try:
                    output.put((True, self.capture(encoded)))
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
                sensor_failures += 1
                if sensor_failures >= 3:
                    raise SkillFailed(
                        f"Basket camera/API failed three stationary attempts: {reason}"
                    )
                continue
            sensor_failures = 0
            if search and value.get("detected") is False:
                raise BasketMissing("Basket not visible in fresh frame")
            try:
                face = face_in_base(value, tilt)
                # Best-effort shared exclusion memory for the optional pickup variant.
                try:
                    from innate_skills.feature_basket_exclusion import remember
                    remember(host, value, tilt)
                except (OSError, ValueError, TypeError, KeyError, SkillFailed) as error:
                    host.logger.info(f"[FeatureSockBox] could not share basket pose: {error}")
                return face
            except (ValueError, TypeError, KeyError) as exc:
                reason = str(exc)
                host.logger.info(f"[FeaturePose] stationary retry {attempts}: {reason}")
                # Like ArUco's final-pose observation, wait for a usable fresh
                # view rather than aborting on three transient pose failures.
                # fresh_odom above enforces the existing phase deadline.
        raise SkillFailed(
            f"No usable basket pose after three stationary attempts: {reason}"
        )


class FeatureController(PointController):
    def __init__(self, host, timeout=50):
        super().__init__(host)
        self.deadline = time.monotonic() + timeout
        self.follower.final_approach()

    def fresh_odom(self):
        if time.monotonic() > self.deadline:
            raise SkillFailed("Feature basket alignment timed out; refusing release")
        return super().fresh_odom()


def find_features(host, observer, controller, speed=0.4):
    """Search continuously; stop and reobserve before using a metric pose."""
    swept = 0.0
    last = controller.fresh_odom()
    old_speed = controller.follower.max_angular
    controller.follower.max_angular = speed

    def tick():
        nonlocal swept, last
        host.check_cancelled()
        now = controller.fresh_odom()
        swept += abs(wrap(now.theta - last.theta))
        last = now
        if swept >= 2 * math.pi:
            raise SkillFailed(
                "Basket not found after looking around; stopped holding sock"
            )
        controller.follower._send_cmd(0.0, -speed)

    try:
        while True:
            # Initial view, reacquisition and API-error retries are stationary.
            try:
                return observer.observe(host, controller, search=True)
            except BasketMissing:
                pass
            last = controller.fresh_odom()
            host.logger.info(
                "[FeatureDock] basket missing; continuous rightward search, no translation"
            )
            while True:
                tick()
                try:
                    found = observer.recognize(host, controller, tick)
                except (SkillFailed, SkillCancelled):
                    raise
                except Exception as exc:
                    # Do not continue blind on transport/decoder failures.
                    # Reuse observe's existing stationary retry behavior.
                    host.logger.info(f"[FeatureSearch] API error; stopping: {exc}")
                    controller.follower._stop()
                    break
                if found:
                    controller.follower._stop()
                    host.logger.info(
                        "[FeatureSearch] basket found; stopping for a fresh stationary pose"
                    )
                    break
    finally:
        controller.follower.max_angular = old_speed
        controller.follower._stop()


def dock_features(host, observer, verify_hold=None):
    """Lock one stationary basket pose; reach the frontal docking point without visual replanning."""
    controller = FeatureController(host, timeout=175)
    try:
        center, normal = find_features(host, observer, controller, speed=0.70)
        odom = controller.fresh_odom()
        origin = odom.x, odom.y, odom.theta
        yaw = math.atan2(normal[1], normal[0])
        basket = in_odom([(center[0], center[1], yaw)], origin)[0]
        final = in_odom([(*(center - host.FINAL_DISTANCE_M * normal), yaw)], origin)[0]
        approach_angle = wrap(yaw - math.atan2(center[1], center[0]))
        host.logger.info(f"[FeatureDock] locked basket_odom={basket}; frontal angle={math.degrees(approach_angle):.1f}; final={final}; no re-detection")
        if abs(approach_angle) > math.radians(20):
            # Match ArUco's frontal staging distance, using only the locked pose.
            staging = in_odom([(*(center - 0.26 * normal), yaw)], origin)[0]
            host.logger.info(f"[FeatureDock] frontal staging target={staging}")
            controller.drive(staging)
            host.mobility.stop()
            controller.turn(basket[2])
        # A second segment finishes along the face normal rather than the
        # original oblique camera ray. drive() handles its existing 4cm tolerance.
        host.logger.info(f"[FeatureDock] final docking target={final}")
        controller.drive(final)
        host.mobility.stop()
        controller.turn(basket[2])
        if verify_hold is not None:
            verify_hold()
        host.check_cancelled()
        now = controller.fresh_odom()
        c, s = math.cos(now.theta), math.sin(now.theta)
        dx, dy = basket[0] - now.x, basket[1] - now.y
        center = np.array([c * dx + s * dy, -s * dx + c * dy])
        angle = basket[2] - now.theta
        normal = np.array([math.cos(angle), math.sin(angle)])
        host.logger.info(f"[FeatureDock] arrived; release from locked pose center={center.tolist()} normal={normal.tolist()}")
        return center, normal
    finally:
        controller.close()
