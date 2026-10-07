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
        logger=Mock(),
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

    def test_lower_ranked_valid_pose_can_recover_recognition_only_best(self):
        good = detection()
        result = detection(pose=None)
        result["candidates"] = [result["best"], good["best"]]
        center, normal = bf.face_in_base(result, 0)
        np.testing.assert_allclose((center, normal), bf.face_in_base(good, 0))

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
            "dock_features_via_point",
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
        self.h.main_image = NS(jpeg=b"initial")
        self.h.main_highres_image = None

        def advance(_):
            self.h.main_image = NS(jpeg=b"next")
            time.sleep(0.001)  # test-only worker scheduling

        self.h.sleep = advance
        self.odom = NS(x=0, y=0, theta=0)
        self.controller = NS(fresh_odom=Mock(side_effect=lambda: self.odom))
        self.observer = bf.FeatureObserver(api_url="")

    def test_three_missing_results_fail_without_moving(self):
        self.observer.capture = Mock(return_value={"detected": False})
        with self.assertRaisesRegex(SkillFailed, "three stationary"):
            self.observer.observe(self.h, self.controller)
        self.assertEqual(self.observer.capture.call_count, 3)
        self.h.mobility.send_cmd_vel.assert_not_called()

    def test_search_reports_missing_on_first_fresh_frame(self):
        self.observer.capture = Mock(return_value={"detected": False})
        with self.assertRaises(bf.BasketMissing):
            self.observer.observe(self.h, self.controller, search=True)
        self.observer.capture.assert_called_once()

    def test_recognized_bad_pose_waits_past_three_frames_then_recovers(self):
        self.observer.capture = Mock(
            side_effect=[detection(pose=None)] * 4 + [detection()]
        )
        center, normal = self.observer.observe(self.h, self.controller, search=True)
        self.assertEqual(self.observer.capture.call_count, 5)
        self.assertGreater(center[0], 0)
        self.h.mobility.send_cmd_vel.assert_not_called()

    def test_persistent_bad_pose_obeys_existing_phase_deadline(self):
        self.observer.capture = Mock(return_value=detection(pose=None))

        def odom():
            if self.observer.capture.call_count >= 4:
                raise SkillFailed("Feature basket alignment timed out")
            return self.odom

        self.controller.fresh_odom.side_effect = odom
        with self.assertRaisesRegex(SkillFailed, "alignment timed out"):
            self.observer.observe(self.h, self.controller, search=True)
        self.h.mobility.send_cmd_vel.assert_not_called()

    def test_api_errors_remain_bounded_even_during_search(self):
        self.observer.capture = Mock(side_effect=TimeoutError("API unavailable"))
        with self.assertRaisesRegex(SkillFailed, "camera/API failed three"):
            self.observer.observe(self.h, self.controller, search=True)
        self.assertEqual(self.observer.capture.call_count, 3)

    def test_error_retries_then_fresh_pose(self):
        self.observer.capture = Mock(side_effect=[TimeoutError("frame"), detection()])
        center, normal = self.observer.observe(self.h, self.controller)
        self.assertEqual(self.observer.capture.call_count, 2)
        self.assertGreater(center[0], 0)

    def test_moved_robot_discards_result(self):
        def capture(encoded):
            self.odom = NS(x=self.odom.x + 0.03, y=0, theta=0)
            return detection()

        self.observer.capture = capture
        with self.assertRaisesRegex(SkillFailed, "moved during"):
            self.observer.observe(self.h, self.controller)

    def test_cancel_does_not_wait_for_inference(self):
        started = threading.Event()
        finish = threading.Event()

        def capture(encoded):
            started.set()
            finish.wait(3)
            return detection()

        self.observer.capture = capture

        def sleep(_):
            self.h.main_image = NS(jpeg=b"next")
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

    def test_stream_uses_new_highres_original_jpeg(self):
        old = NS(jpeg=b"old")
        fresh = NS(jpeg=b"original highres")
        self.h.main_highres_image = old
        self.h.sleep = lambda _: setattr(self.h, "main_highres_image", fresh)
        encoded = self.observer.fresh_frame(self.h, self.controller)
        self.assertIs(encoded, fresh.jpeg)

    def test_frozen_stream_times_out(self):
        self.h.sleep = Mock()
        with patch.object(bf.time, "monotonic", side_effect=[0, 0, 3.1]):
            with self.assertRaisesRegex(SkillFailed, "stream stopped"):
                self.observer.fresh_frame(self.h, self.controller)

    def test_cancel_during_frame_wait(self):
        self.h.sleep = Mock(side_effect=SkillCancelled("stop"))
        with self.assertRaises(SkillCancelled):
            self.observer.fresh_frame(self.h, self.controller)

    def test_frame_is_selected_after_settle(self):
        seen = []
        def advance(duration):
            seen.append(duration)
            self.h.main_image = NS(jpeg=str(len(seen)).encode())
        self.h.sleep = advance
        self.observer.capture = Mock(return_value=detection())
        self.observer.observe(self.h, self.controller)
        self.assertEqual(seen[:2], [0.3, 0.02])
        self.observer.capture.assert_called_once_with(b"2")



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
                NS(
                    observe=lambda *args, **kwargs: (
                        np.array([0.16, 0]),
                        np.array([1, 0]),
                    )
                ),
            )
            factory.return_value.drive.assert_not_called()
            factory.return_value.close.assert_called_once()

    def test_deadline_rejects_even_if_odometry_exists(self):
        with patch.object(bf.PointController, "__init__", return_value=None):
            c = object.__new__(bf.FeatureController)
            c.deadline = 0
            with self.assertRaisesRegex(SkillFailed, "timed out"):
                c.fresh_odom()


class SearchTests(unittest.TestCase):
    def controller(self):
        c = Mock()
        state = NS(x=0, y=0, theta=0)
        c.fresh_odom.side_effect = lambda: NS(x=state.x, y=state.y, theta=state.theta)
        c.turn.side_effect = lambda angle: setattr(state, "theta", angle)
        c.follower.max_angular = 0.6
        return c

    def test_absent_basket_turns_right_then_reobserves_without_driving(self):
        c = self.controller()
        observer = Mock()
        expected = (np.array([0.3, 0]), np.array([1, 0]))
        observer.observe.side_effect = [bf.BasketMissing("missing"), expected]
        result = bf.find_features(host(), observer, c, speed=0.7)
        self.assertIs(result, expected)
        self.assertLess(c.turn.call_args.args[0], 0)
        c.drive.assert_not_called()
        self.assertEqual(observer.observe.call_count, 2)
        self.assertEqual(c.follower.max_angular, 0.6)
        c.follower._stop.assert_called()

    def test_real_inherited_turn_controller_emits_rightward_commands(self):
        h = host()
        state = NS(now=100.0, theta=0.0, velocity=0.0)
        commands = []

        def send(linear_x, angular_z, duration):
            commands.append((linear_x, angular_z))
            state.velocity = angular_z

        def sleep(seconds):
            state.theta += state.velocity * seconds
            state.now += seconds
            if state.now > 110:
                self.fail("Search turn did not finish")

        h.mobility.send_cmd_vel.side_effect = send
        h.mobility.stop.side_effect = lambda: setattr(state, "velocity", 0)
        h.sleep = sleep
        observer = Mock()
        observer.observe.side_effect = [
            bf.BasketMissing("missing"),
            (np.array([0.4, 0]), np.array([1, 0])),
        ]
        with patch.object(bf.time, "monotonic", side_effect=lambda: state.now):
            c = bf.FeatureController(h)
            c.fresh_odom = lambda: NS(x=0, y=0, theta=state.theta)
            bf.find_features(h, observer, c)
        self.assertTrue(commands)
        self.assertTrue(
            all(linear == 0 and angular <= 0 for linear, angular in commands)
        )
        self.assertLess(state.theta, -0.3)
        self.assertEqual(state.velocity, 0)

    def test_full_scan_stops_without_translation(self):
        c = self.controller()
        observer = Mock()
        observer.observe.side_effect = bf.BasketMissing("missing")
        with self.assertRaisesRegex(SkillFailed, "after looking around"):
            bf.find_features(host(), observer, c)
        self.assertGreater(c.turn.call_count, 10)
        self.assertLess(c.turn.call_count, 20)
        c.drive.assert_not_called()

    def test_sensor_error_and_cancel_do_not_initiate_search(self):
        for error in [SkillFailed("camera stale"), SkillCancelled("stop")]:
            c = self.controller()
            with self.assertRaises(type(error)):
                bf.find_features(host(), NS(observe=Mock(side_effect=error)), c)
            c.turn.assert_not_called()
            c.follower._stop.assert_called()

    def test_oblique_face_uses_same_26cm_waypoint_as_aruco(self):
        center = np.array([0.6, 0])
        normal = np.array([math.cos(0.6), math.sin(0.6)])
        c = self.controller()
        with (
            patch.object(bf, "FeatureController", return_value=c),
            patch.object(bf, "find_features", return_value=(center, normal)),
        ):
            bf.dock_features_via_point(host(), Mock())
            target = c.drive.call_args.args[0]
            np.testing.assert_allclose(target[:2], center - 0.26 * normal)
            c.turn.assert_called_once()

    def test_frontal_face_skips_waypoint_like_aruco(self):
        c = self.controller()
        with (
            patch.object(bf, "FeatureController", return_value=c),
            patch.object(
                bf, "find_features", return_value=(np.array([0.6, 0]), np.array([1, 0]))
            ),
        ):
            bf.dock_features_via_point(host(), Mock())
            c.drive.assert_not_called()

    def test_arm_motion_and_heights_are_inherited_from_aruco(self):
        from innate_skills.drop_in_box_aruco import DropInBoxAruco

        for attribute in [
            "RELEASE_Z",
            "LOWER_RELEASE_Z",
            "CLEARANCE_Z",
            "_reach_over_box",
            "_release_at",
            "_retract",
            "_p",
        ]:
            self.assertEqual(
                getattr(skill.DropInBoxFeatures, attribute),
                getattr(DropInBoxAruco, attribute),
            )


class RuntimeConfigTests(unittest.TestCase):
    def test_default_uses_saved_endpoint_and_token_explicit_local_override_wins(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder)
            (assets / "calibration.json").write_bytes(
                (bf.ASSETS / "calibration.json").read_bytes()
            )
            token = assets / "test.token"
            token.write_text("test-only-token\n")
            (assets / "runtime.json").write_text(
                json.dumps(
                    {"api_url": "http://test-host:9071", "token_file": str(token)}
                )
            )
            observer = bf.FeatureObserver(assets)
            self.assertEqual(observer.api_url, "http://test-host:9071")
            self.assertEqual(observer.api_token, "test-only-token")
            token.unlink()
            self.assertEqual(bf.FeatureObserver(assets, api_url="").api_url, "")
            with self.assertRaises(FileNotFoundError):
                bf.FeatureObserver(assets)


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

        server = make_server(bf.ASSETS, port=0, auth_token="test-only-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        image = np.zeros((720, 1280, 3), np.uint8)
        encoded = cv2.imencode(".jpg", image)[1].tobytes()
        observer = bf.FeatureObserver(
            api_url=f"http://127.0.0.1:{server.server_port}",
            api_token="test-only-token",
        )
        try:
            self.assertFalse(observer.capture(encoded)["detected"])
            self.assertIsNone(observer.detector)
            with patch.object(
                bf.json,
                "load",
                return_value={"calibration_id": "wrong", "image_sha256": "wrong"},
            ):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    observer.capture(encoded)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
