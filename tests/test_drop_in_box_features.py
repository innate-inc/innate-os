"""Real orchestration and geometry, with hardware calls replaced by recorders."""

import math
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "workspace"), str(ROOT / "ros2_ws/src/brain/brain_client")]
from innate_skills import box_features as bf  # noqa: E402 - repository source imports
from innate_skills import drop_in_box_features as skill  # noqa: E402 - repository source imports
from innate.exceptions import SkillCancelled, SkillFailed  # noqa: E402 - repository source imports


def detection(center=(0, 0, 0.5), normal=(0, 0, 1), **kwargs):
    return {
        "detected": True,
        "best": {
            "pose_ambiguous": False,
            "pose": {
                "face_center_camera_m": list(center),
                "face_normal_camera": list(normal),
                "reprojection_rms_px": 1,
            },
            **kwargs,
        },
    }


def host():
    h = NS(
        mobility=Mock(),
        head=Mock(),
        head_position=NS(pitch_degrees=-20),
        joint_states=NS(position=[0] * 6),
        overlay=Mock(),
        _p=skill.DropInBoxFeatures._p,
        FINAL_DISTANCE_M=0.16,
        DROP_RIGHT_M=0.08,
        CLEARANCE_Z=0.30,
        _select_drop_pitch=Mock(),
        _secure_grip=Mock(return_value=True),
        _holding=Mock(return_value=True),
        _retract=Mock(),
        _detect_px=Mock(),
        check_cancelled=Mock(),
        sleep=Mock(),
    )
    h.wait_for = lambda read, **kw: read()

    def release(*args):
        h._released = True
        h._over_rim = False

    h._release_at = Mock(side_effect=release)
    return h


class GeometryTests(unittest.TestCase):
    def test_optical_right_is_base_right_and_normal_is_inward(self):
        center, normal = bf.face_in_base(detection((0.1, 0, 0.5)), 0)
        self.assertLess(center[1], 0)
        np.testing.assert_allclose(normal, [1, 0])
        np.testing.assert_allclose(
            bf.release_target(center, normal) - center, [0.13, -0.08]
        )

    def test_rotated_target_is_face_relative(self):
        n = np.array([math.cos(0.6), math.sin(0.6)])
        center = np.array([0.3, 0.1])
        target = np.array(bf.release_target(center, n))
        self.assertAlmostEqual(float((target - center) @ n), 0.13)
        self.assertAlmostEqual(float((target - center) @ np.array([n[1], -n[0]])), 0.08)

    def test_missing_ambiguous_nonfinite_backwards_rejected(self):
        for value in [
            {"detected": False},
            detection(pose_ambiguous=True),
            detection(pose=None),
            detection((0, 0, float("nan"))),
            detection(normal=(0, 0, -1)),
            detection(normal=(0, 0, 2)),
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bf.face_in_base(value, 0)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.h = host()
        self.context = []
        for name in [
            "FeatureObserver",
            "lift_vertical",
            "dock_features",
            "fresh_sock_held",
            "FloorApproach",
            "retract_to_rest",
        ]:
            p = patch.object(skill, name)
            m = p.start()
            self.addCleanup(p.stop)
            setattr(self, name, m)
        self.dock_features.return_value = (np.array([0.16, 0]), np.array([1, 0]))
        self.fresh_sock_held.return_value = True

        def dock(h, observer, verify_hold=None):
            if verify_hold:
                verify_hold()
            return self.dock_features.return_value

        self.dock_features.side_effect = dock
        self.FloorApproach.return_value.drive.return_value = True

    def run_skill(self):
        return skill.DropInBoxFeatures.execute(self.h)

    def test_success_uses_live_geometry_and_clears_before_retreat(self):
        result = self.run_skill()
        np.testing.assert_allclose(self.h._box_release_xy, [0.29, -0.08])
        self.h._release_at.assert_called_once_with(0.16, 0)
        self.FloorApproach.return_value.drive.assert_called_once_with(-0.15)
        self.assertIn("not visually verified", result)
        self.h.mobility.stop.assert_called()
        self.h._retract.assert_called_once()

    def test_no_pose_holds_sock_without_release_or_retreat(self):
        self.dock_features.side_effect = SkillFailed("No basket")
        with self.assertRaises(SkillFailed):
            self.run_skill()
        self.h._release_at.assert_not_called()
        self.FloorApproach.assert_not_called()
        self.assertTrue(self.h._vertical_lift_pending)
        self.h.mobility.stop.assert_called()

    def test_empty_grip_never_approaches(self):
        self.h._holding.return_value = False
        with self.assertRaises(SkillFailed):
            self.run_skill()
        self.dock_features.assert_not_called()
        self.h._release_at.assert_not_called()

    def test_sock_lost_during_approach_no_release(self):
        self.fresh_sock_held.return_value = False
        with self.assertRaises(SkillFailed):
            self.run_skill()
        self.h._release_at.assert_not_called()

    def test_bad_ik_before_motion(self):
        self.h._select_drop_pitch.side_effect = SkillFailed("unreachable")
        with self.assertRaises(SkillFailed):
            self.run_skill()
        self.h.head.set_position.assert_not_called()
        self.lift_vertical.assert_not_called()
        self.h._release_at.assert_not_called()

    def test_head_timeout_never_lifts(self):
        self.h.wait_for = Mock(side_effect=[self.h.joint_states, None])
        with self.assertRaises(SkillFailed):
            self.run_skill()
        self.lift_vertical.assert_not_called()

    def test_uncleared_arm_never_retreats(self):
        self.h._release_at.side_effect = None
        with self.assertRaisesRegex(SkillFailed, "not cleared"):
            self.run_skill()
        self.FloorApproach.assert_not_called()
        self.retract_to_rest.assert_not_called()

    def test_cancellation_before_release_preserved(self):
        self.dock_features.side_effect = SkillCancelled("stop")
        with self.assertRaises(SkillCancelled):
            self.run_skill()
        self.h._release_at.assert_not_called()
        self.h.mobility.stop.assert_called()


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.h = host()
        self.h.sleep = lambda _: time.sleep(0.001)  # test-only worker scheduling
        self.odom = NS(x=0, y=0, theta=0)
        self.controller = NS(fresh_odom=Mock(side_effect=lambda: self.odom))
        self.observer = bf.FeatureObserver(api_url="")

    def test_three_missing_results_fail_without_moving(self):
        self.observer.capture = Mock(return_value={"detected": False})
        with self.assertRaisesRegex(SkillFailed, "three stationary"):
            self.observer.observe(self.h, self.controller)
        self.assertEqual(self.observer.capture.call_count, 3)
        self.h.mobility.send_cmd_vel.assert_not_called()

    def test_error_retries_then_fresh_pose(self):
        self.observer.capture = Mock(side_effect=[TimeoutError("frame"), detection()])
        center, normal = self.observer.observe(self.h, self.controller)
        self.assertEqual(self.observer.capture.call_count, 2)
        self.assertGreater(center[0], 0)

    def test_moved_robot_discards_result(self):
        def capture():
            self.odom = NS(x=self.odom.x + 0.03, y=0, theta=0)
            return detection()

        self.observer.capture = capture
        with self.assertRaisesRegex(SkillFailed, "moved during"):
            self.observer.observe(self.h, self.controller)

    def test_cancel_does_not_wait_for_inference(self):
        started = threading.Event()
        finish = threading.Event()

        def capture():
            started.set()
            finish.wait(3)
            return detection()

        self.observer.capture = capture

        def sleep(_):
            if started.is_set():
                raise SkillCancelled("stop")

        self.h.sleep = sleep
        before = time.monotonic()
        try:
            with self.assertRaises(SkillCancelled):
                self.observer.observe(self.h, self.controller)
            self.assertLess(time.monotonic() - before, 0.5)
        finally:
            finish.set()


class DockTests(unittest.TestCase):
    def test_bounded_translation_then_reobserve_before_release(self):
        h = host()
        observer = Mock()
        observer.observe.side_effect = [
            (np.array([0.6, 0.1]), np.array([1, 0])),
            (np.array([0.16, 0]), np.array([1, 0])),
        ]
        with patch.object(bf, "FeatureController") as factory:
            c = factory.return_value
            c.fresh_odom.return_value = NS(x=0, y=0, theta=0)
            bf.dock_features(h, observer)
            self.assertEqual(observer.observe.call_count, 2)
            x, y, _ = c.drive.call_args.args[0]
            self.assertLessEqual(math.hypot(x, y), 0.150001)
            c.close.assert_called_once()

    def test_target_loss_after_grip_check_never_returns_release_pose(self):
        observer = Mock()
        observer.observe.side_effect = [
            (np.array([0.16, 0]), np.array([1, 0])),
            SkillFailed("gone"),
        ]
        verify = Mock()
        with patch.object(bf, "FeatureController") as factory:
            with self.assertRaises(SkillFailed):
                bf.dock_features(host(), observer, verify_hold=verify)
            verify.assert_called_once()
            factory.return_value.drive.assert_not_called()
            factory.return_value.close.assert_called_once()

    def test_grip_rechecked_after_further_movement(self):
        observer = Mock()
        aligned = (np.array([0.16, 0]), np.array([1, 0]))
        observer.observe.side_effect = [
            aligned,
            (np.array([0.4, 0]), np.array([1, 0])),
            aligned,
            aligned,
        ]
        verify = Mock()
        with patch.object(bf, "FeatureController") as factory:
            factory.return_value.fresh_odom.return_value = NS(x=0, y=0, theta=0)
            bf.dock_features(host(), observer, verify_hold=verify)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(observer.observe.call_count, 4)

    def test_aligned_pose_never_commands_drive(self):
        with patch.object(bf, "FeatureController") as factory:
            bf.dock_features(
                host(),
                NS(observe=lambda *args: (np.array([0.16, 0]), np.array([1, 0]))),
            )
            factory.return_value.drive.assert_not_called()
            factory.return_value.close.assert_called_once()

    def test_deadline_rejects_even_if_odometry_exists(self):
        with patch.object(bf.PointController, "__init__", return_value=None):
            c = object.__new__(bf.FeatureController)
            c.deadline = 0
            with self.assertRaisesRegex(SkillFailed, "timed out"):
                c.fresh_odom()


class CaptureTests(unittest.TestCase):
    def test_queued_frame_before_stop_is_ignored(self):
        import base64
        import json
        import cv2
        from innate_skills.basket_capture import live_frame

        camera = bf.Camera.load(bf.ASSETS / "calibration.json")
        now = time.time()

        def frame(stamp):
            return json.dumps(
                {
                    "op": "publish",
                    "topic": "/mars/main_camera/left/image_highres/compressed",
                    "msg": {
                        "header": {
                            "stamp": {
                                "sec": int(stamp),
                                "nanosec": int((stamp % 1) * 1e9),
                            }
                        },
                        "data": base64.b64encode(
                            cv2.imencode(".jpg", np.zeros((720, 1280, 3), np.uint8))[1]
                        ).decode(),
                    },
                }
            )

        info = json.dumps(
            {
                "op": "publish",
                "topic": "/mars/main_camera/left/camera_info",
                "msg": {
                    "width": 640,
                    "height": 480,
                    "distortion_model": "plumb_bob",
                    "k": camera.matrix.flatten().tolist(),
                    "d": camera.distortion.tolist(),
                },
            }
        )
        ws = Mock()
        ws.recv.side_effect = [info, frame(now - 1), frame(now)]
        _, meta, _ = live_frame(ws, camera, not_before=now - 0.1)
        self.assertEqual(ws.recv.call_count, 3)
        self.assertLess(meta["age_at_receive_s"], 0.5)

    def test_live_calibration_mismatch_rejected(self):
        import json
        from innate_skills.basket_capture import live_frame

        camera = bf.Camera.load(bf.ASSETS / "calibration.json")
        ws = Mock()
        ws.recv.return_value = json.dumps(
            {
                "op": "publish",
                "topic": "/mars/main_camera/left/camera_info",
                "msg": {"width": 640, "height": 480, "distortion_model": "wrong"},
            }
        )
        with self.assertRaisesRegex(ValueError, "calibration changed"):
            live_frame(ws, camera)

    def test_remote_client_real_http_and_wrong_identity_rejection(self):
        import cv2

        sys.path.insert(0, str(ROOT / "scripts"))
        from basket_pose_api import make_server

        server = make_server(bf.ASSETS, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        image = np.zeros((720, 1280, 3), np.uint8)
        encoded = cv2.imencode(".jpg", image)[1].tobytes()
        now = time.time()
        meta = {"header": {"stamp": {"sec": int(now), "nanosec": int(now % 1 * 1e9)}}}
        observer = bf.FeatureObserver(api_url=f"http://127.0.0.1:{server.server_port}")
        try:
            with (
                patch.object(bf.websocket, "create_connection"),
                patch.object(bf, "live_frame", return_value=(image, meta, encoded)),
            ):
                self.assertFalse(observer.capture()["detected"])
                with patch.object(
                    bf.json,
                    "load",
                    return_value={"calibration_id": "wrong", "image_sha256": "wrong"},
                ):
                    with self.assertRaisesRegex(ValueError, "does not match"):
                        observer.capture()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
