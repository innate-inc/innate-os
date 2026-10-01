import ast
import importlib.util
import json
import math
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import cv2
import numpy as np

ROOT = Path(__file__).parents[1] / "innate_skills"
REPO = ROOT.parents[1]


def load_marker():
    spec = importlib.util.spec_from_file_location(
        "geometry", REPO / "ros2_ws/src/brain/brain_client/innate/geometry.py"
    )
    geo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(geo)
    env = dict(
        math=math,
        np=np,
        cv2=cv2,
        json=json,
        Path=Path,
        time=time,
        SkillFailed=RuntimeError,
        __file__=str(ROOT / "box_marker.py"),
        vision=NS(b64_to_gray=lambda x: x.gray),
    )
    env.update(vars(geo))
    env["__file__"] = str(ROOT / "box_marker.py")
    env.update(Skill=object, SkillReturn=str, MainImage=object, Mobility=object, resource=lambda f: f)
    follow = ast.parse((ROOT / "follow_aruco.py").read_text())
    follow.body = [n for n in follow.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    exec(compile(follow, "follow_aruco", "exec"), env)
    tree = ast.parse((ROOT / "box_marker.py").read_text())
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, "box_marker", "exec"), env)
    return env


class MarkerGeometryTests(unittest.TestCase):
    def setUp(self):
        self.e = load_marker()

    def test_tag_detection_id_and_pose_on_generated_image(self):
        im = np.full((480, 640), 255, np.uint8)
        tag = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 1, 100)
        im[180:280, 270:370] = tag
        # Detection exercised; pose separately uses geometrically consistent projection.
        d = self.e["MarkerPose"](1, 0.06, -12)
        corners, ids, _ = d.detector.detectMarkers(im)
        self.assertEqual(ids.ravel().tolist(), [1])
        wrong = self.e["MarkerPose"](2, 0.06, -12)
        self.assertIsNone(wrong.detect(im))
        im[50:150, 50:150] = tag
        self.assertIsNone(d.detect(im))  # duplicate ID

    def test_metric_projection_round_trip(self):
        d = self.e["MarkerPose"](1, 0.06, -12)
        r = np.array([3.0, 0.12, 0.1])
        t = np.array([0.04, 0.04, 0.45])
        px = cv2.projectPoints(d.object_points, r, t, self.e["camera_matrix"](), np.zeros(5))[0].reshape(4, 2)
        pose = d.from_corners(px)
        self.assertIsNotNone(pose)
        camera = np.linalg.inv(self.e["camera_in_base"](-12)) @ pose
        np.testing.assert_allclose(camera[:3, 3], t, atol=1e-4)
        self.assertIsNone(d.from_corners(np.array([[0, 0], [10, 0], [10, 10], [0, 10]])))

    def config(self):
        camera = np.eye(4)
        camera[:3, :3] = cv2.Rodrigues(np.array([math.pi, 0.0, 0.0]))[0]
        camera[:3, 3] = [0.0, 0.0, 0.35]
        taught = self.e["camera_in_base"](-12) @ camera
        return dict(
            version=1,
            dictionary="DICT_4X4_50",
            marker_id=1,
            marker_size_m=0.06,
            image_size=[640, 480],
            camera_matrix=self.e["camera_matrix"]().tolist(),
            head_tilt_deg=-12,
            near_xy=[0.23, 0],
            base_from_marker=taught.tolist(),
        )

    def test_invalid_config_rejected(self):
        self.e["validate_config"](self.config())
        for key, value in [
            ("marker_id", True),
            ("marker_size_m", float("nan")),
            ("head_tilt_deg", float("nan")),
            ("near_xy", [0.6, 0]),
            ("base_from_marker", np.zeros((4, 4)).tolist()),
        ]:
            c = self.config()
            c[key] = value
            with self.assertRaises(ValueError):
                self.e["validate_config"](c)

    def test_controller_requires_fresh_frames_and_stops_on_loss_cancel(self):
        for case in ["arrival", "stale", "lost", "cancel"]:
            clock = [0.0]
            self.e["time"] = NS(monotonic=lambda clock=clock: clock[0])
            h = NS(main_image=NS(gray="frame"), mobility=Mock(), logger=Mock(), check_cancelled=Mock())

            def sleep(dt, clock=clock, case=case, h=h):
                clock[0] += dt
                if case != "stale":
                    h.main_image = NS(gray="frame")
                if case == "cancel":
                    raise RuntimeError("cancel")

            h.sleep = sleep
            dock = self.e["MarkerDock"](h, self.config())
            dock.detector = NS(detect_quad=Mock(return_value=None if case == "lost" else dock.target_quad))
            if case == "arrival":
                self.assertEqual(dock.run(), (0.23, 0))
            else:
                with self.assertRaises(RuntimeError):
                    dock.run()
            h.mobility.stop.assert_called()
            self.assertTrue(all(c.kwargs["linear_x"] == 0 for c in h.mobility.send_cmd_vel.call_args_list))
            if case != "lost":
                self.assertTrue(all(c.kwargs["angular_z"] == 0 for c in h.mobility.send_cmd_vel.call_args_list))


class FrontTagRegressionTests(MarkerGeometryTests):
    def test_teaching_farther_away_creates_near_edge_goal(self):
        c = self.config()
        pose = np.eye(4)
        pose[:3, 3] = [0.5303, -0.1118, 0.0606]
        c["base_from_marker"] = pose.tolist()
        updated = self.e["front_tag_target"](c)
        np.testing.assert_allclose(np.array(updated["base_from_marker"])[:3, 3], [0.23, 0, 0.0606])
        np.testing.assert_allclose(np.array(updated["observed_base_from_marker"])[:3, 3], pose[:3, 3])
        self.assertEqual(updated["version"], 2)
        self.assertEqual(c["version"], 1)


class ImageServoTests(unittest.TestCase):
    def test_real_decoder_reaches_goal_without_pose_estimation(self):
        e = load_marker()
        helper = MarkerGeometryTests()
        helper.e = e
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        h = NS(main_image=NS(gray=None), mobility=Mock(), logger=Mock(), check_cancelled=Mock())
        dock = e["MarkerDock"](h, helper.config())
        tag = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 1, 100)
        transform = cv2.getPerspectiveTransform(
            np.float32([[0, 0], [99, 0], [99, 99], [0, 99]]), dock.target_quad.astype(np.float32)
        )
        image = cv2.warpPerspective(tag, transform, (640, 480), borderValue=255)

        def sleep(dt):
            clock[0] += dt
            h.main_image = NS(gray=image)

        h.sleep = sleep
        dock.detector.from_corners = Mock(side_effect=AssertionError("No pose fitting allowed during approach"))
        self.assertEqual(dock.run(), (0.23, 0))
        dock.detector.from_corners.assert_not_called()
        self.assertTrue(
            all(
                c.kwargs["linear_x"] == 0 and c.kwargs["angular_z"] == 0 for c in h.mobility.send_cmd_vel.call_args_list
            )
        )

    def test_wrong_vertical_alignment_cannot_release_even_at_correct_size(self):
        e = load_marker()
        helper = MarkerGeometryTests()
        helper.e = e
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        h = NS(main_image=NS(gray="frame"), mobility=Mock(), logger=Mock(), check_cancelled=Mock())

        def sleep(dt):
            clock[0] += dt
            h.main_image = NS(gray="frame")

        h.sleep = sleep
        dock = e["MarkerDock"](h, helper.config())
        q = dock.target_quad.copy()
        q[:, 1] += 25
        dock.detector = NS(detect_quad=lambda im: q)
        with self.assertRaisesRegex(RuntimeError, "not aligned"):
            dock.run()
        h.mobility.stop.assert_called()


class StraightApproachTests(unittest.TestCase):
    def test_recorded_final_alignment_is_accepted(self):
        e = load_marker()
        helper = MarkerGeometryTests()
        helper.e = e
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        h = NS(main_image=NS(gray=None), mobility=Mock(), logger=Mock(), check_cancelled=Mock())
        dock = e["MarkerDock"](h, helper.config())
        goal = dock.target_quad
        center = goal.mean(axis=0)
        q = center + (goal - center) * (1 - 0.039) + [-2.3, -12.3]
        h.sleep = lambda dt: (clock.__setitem__(0, clock[0] + dt), setattr(h, "main_image", NS(gray=q)))
        dock.detector = NS(detect_quad=lambda gray: gray)
        self.assertEqual(dock.run(), (0.23, 0))
        self.assertTrue(
            all(
                c.kwargs["linear_x"] == 0 and c.kwargs["angular_z"] == 0 for c in h.mobility.send_cmd_vel.call_args_list
            )
        )

    def test_offset_approach_with_camera_delay_and_wheel_response(self):
        for start in [(-0.45, 0.06, 0.087), (-0.55, 0.18, -0.15), (-0.55, -0.18, 0.15)]:
            with self.subTest(start=start):
                self.simulate_offset_approach(start)

    def simulate_offset_approach(self, start):
        e = load_marker()
        helper = MarkerGeometryTests()
        helper.e = e
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        # Start 45cm back, 6cm to the left, with 5 degrees initial heading.
        state = np.array(start, dtype=float)
        command = np.zeros(2)
        velocity = np.zeros(2)
        h = NS(main_image=NS(gray=None), mobility=Mock(), logger=Mock(), check_cancelled=Mock())
        dock = e["MarkerDock"](h, helper.config())
        taught = np.array(helper.config()["base_from_marker"])
        points = dock.detector.object_points
        yaw_commands = []

        def send(linear_x, angular_z, duration):
            command[:] = [linear_x, angular_z]
            yaw_commands.append(angular_z)

        h.mobility.send_cmd_vel.side_effect = send
        h.mobility.stop.side_effect = lambda: command.fill(0)

        def frame():
            x, y, a = state
            world = np.eye(4)
            world[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
            world[:2, 3] = [x, y]
            camera = np.linalg.inv(e["camera_in_base"](-12)) @ np.linalg.inv(world) @ taught
            p = points @ camera[:3, :3].T + camera[:3, 3]
            pixels = p @ e["camera_matrix"]().T
            return pixels[:, :2] / pixels[:, 2:]

        def sleep(dt):
            old = int(clock[0] / 0.1)
            clock[0] += dt
            velocity[:] += (command - velocity) * (1 - math.exp(-dt / 0.08))
            state[0] += velocity[0] * math.cos(state[2]) * dt
            state[1] += velocity[0] * math.sin(state[2]) * dt
            state[2] += velocity[1] * dt
            if int(clock[0] / 0.1) > old:
                h.main_image = NS(gray=frame())

        h.sleep = sleep
        dock.detector = NS(detect_quad=lambda gray: gray)
        self.assertEqual(dock.run(), (0.23, 0))
        significant = [math.copysign(1, w) for w in yaw_commands if abs(w) > 0.04]
        turns = sum(a != b for a, b in zip(significant, significant[1:], strict=False))
        self.assertLessEqual(turns, 2)
        self.assertLess(clock[0], 25)


class FollowReuseTests(unittest.TestCase):
    def test_controller_methods_are_the_actual_follower_methods(self):
        e = load_marker()
        for method in ["_drive_toward", "_send_cmd", "_stop", "_smooth"]:
            self.assertIs(getattr(e["MarkerFollower"], method), getattr(e["FollowAruco"], method))

    def test_default_follower_commands_preserved(self):
        e = load_marker()
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        m = Mock()
        f = e["MarkerFollower"](m)
        f.max_linear = 0.3
        f.max_reverse = 0.1
        f.max_angular = 0.8
        f.linear_slew = 0.5
        f.angular_slew = 2.0
        q = np.array([[390, 200], [430, 200], [430, 240], [390, 240]], dtype=float)
        for _ in range(5):
            f._drive_toward(q)
            clock[0] += 0.1
        actual = [(c.kwargs["linear_x"], c.kwargs["angular_z"]) for c in m.send_cmd_vel.call_args_list]
        np.testing.assert_allclose(
            actual, [(0.05, -0.2), (0.1, -0.4), (0.15, -0.421875), (0.2, -0.421875), (0.25, -0.421875)]
        )

    def test_custom_target_moves_and_turns_simultaneously_with_original_slew(self):
        e = load_marker()
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        m = Mock()
        f = e["MarkerFollower"](m)
        q = np.array([[350, 200], [390, 200], [390, 240], [350, 240]], dtype=float)
        f._drive_toward(q, target_center_x=340, target_size_frac=0.12, size_deadband=0.04)
        v = m.send_cmd_vel.call_args.kwargs
        self.assertGreater(v["linear_x"], 0)
        self.assertLess(v["angular_z"], 0)
        self.assertLessEqual(v["linear_x"], 0.5 * 0.1)
        self.assertLessEqual(abs(v["angular_z"]), 2 * 0.1)


class BoxMotionCapsTests(unittest.TestCase):
    def test_caps_acceleration_braking_and_reverse(self):
        e = load_marker()
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        m = Mock()
        f = e["MarkerFollower"](m)
        previous = (0.0, 0.0)
        for target in [(1.0, 2.0)] * 25 + [(0.0, 0.0)] * 25 + [(-1.0, -2.0)] * 25:
            clock[0] += 0.1
            f._send_cmd(*target)
            cmd = m.send_cmd_vel.call_args.kwargs
            v, w = cmd["linear_x"], cmd["angular_z"]
            self.assertLessEqual(v, 0.15)
            self.assertGreaterEqual(v, -0.08)
            self.assertLessEqual(abs(w), 0.6)
            self.assertLessEqual(
                abs(v - previous[0]), (f.linear_braking if abs(v) < abs(previous[0]) else f.linear_slew) * 0.1 + 1e-9
            )
            self.assertLessEqual(
                abs(w - previous[1]), (f.angular_braking if abs(w) < abs(previous[1]) else f.angular_slew) * 0.1 + 1e-9
            )
            previous = v, w
        f._stop()
        m.stop.assert_called()
        self.assertEqual(f._cmd_linear, 0.0)

    def test_brakes_to_zero_before_gently_reversing(self):
        e = load_marker()
        clock = [0.0]
        e["time"] = NS(monotonic=lambda: clock[0])
        f = e["MarkerFollower"](Mock())
        f._cmd_linear, f._cmd_angular = 0.15, 0.5
        f._last_cmd_time = 0.0
        for _ in range(4):
            clock[0] += 0.1
            f._send_cmd(-0.15, -0.5)
            self.assertGreaterEqual(f._cmd_linear, 0)
        self.assertEqual(f._cmd_linear, 0)
        self.assertLessEqual(abs(f._cmd_angular), f.angular_slew * 0.1 + 1e-9)
        clock[0] += 0.1
        f._send_cmd(-0.15, -0.5)
        self.assertAlmostEqual(f._cmd_linear, -f.linear_slew * 0.1)
        self.assertLessEqual(abs(f._cmd_angular), f.angular_slew * 0.2 + 1e-9)


class BoxSearchTests(unittest.TestCase):
    def test_search_rotates_without_driving_then_approaches(self):
        e = load_marker()
        helper = MarkerGeometryTests()
        helper.e = e
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        h = NS(main_image=NS(gray="frame"), mobility=Mock(), logger=Mock(), check_cancelled=Mock())

        def sleep(dt):
            clock[0] += dt
            h.main_image = NS(gray="frame")

        h.sleep = sleep
        dock = e["MarkerDock"](h, helper.config())
        dock.detector = NS(detect_quad=lambda gray: None if clock[0] < 1 else dock.target_quad)
        self.assertEqual(dock.run(), (0.23, 0))
        commands = [c.kwargs for c in h.mobility.send_cmd_vel.call_args_list]
        self.assertTrue(any(c["angular_z"] > 0 for c in commands))
        self.assertTrue(all(c["linear_x"] == 0 for c in commands))
        self.assertLessEqual(max(c["angular_z"] for c in commands), 0.5)
        h.mobility.stop.assert_called()

    def test_search_does_not_lock_on_one_repeated_frame(self):
        e = load_marker()
        helper = MarkerGeometryTests()
        helper.e = e
        clock = [0.0]
        e["time"] = NS(monotonic=lambda clock=clock: clock[0])
        h = NS(main_image=NS(gray="frame"), mobility=Mock(), logger=Mock(), check_cancelled=Mock())
        h.sleep = lambda dt: clock.__setitem__(0, clock[0] + dt)
        dock = e["MarkerDock"](h, helper.config())
        dock.detector = NS(detect_quad=lambda gray: dock.target_quad)
        with self.assertRaisesRegex(RuntimeError, "Camera stale"):
            dock.run()
        h.mobility.stop.assert_called()


class OvershootRecoveryTests(unittest.TestCase):
    def test_left_overshoot_turns_right_and_recenters(self):
        r = load_marker()["MarkerOvershootRecovery"]()
        for i, du in enumerate([-200, -50, -5, 10]):
            self.assertIsNone(r.update(du, i * 0.1))
        self.assertLess(r.update(45, 0.4), 0)
        self.assertEqual(r.update(5, 0.5), 0)
        self.assertEqual(r.update(4, 0.6), 0)
        self.assertEqual(r.update(3, 0.7), 0)
        self.assertIsNone(r.update(3, 0.8))

    def test_mirror_jitter_loss_and_timeout(self):
        cls = load_marker()["MarkerOvershootRecovery"]
        r = cls()
        self.assertIsNone(r.update(200, 0))
        self.assertGreater(r.update(-45, 0.1), 0)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            r.update(-40, 6)
        r.reset()
        for i, du in enumerate([-10, 10, -7, 7]):
            self.assertIsNone(r.update(du, i))
        r.update(-100, 4)
        r.reset()  # no turning on stale/missing observations
        self.assertIsNone(r.update(100, 5))

    def test_docking_recovery_commands_right_without_forward_motion(self):
        e = load_marker()
        helper = MarkerGeometryTests()
        helper.e = e
        clock = [0.0]
        e["time"] = NS(monotonic=lambda: clock[0])
        h = NS(main_image=NS(gray=None), mobility=Mock(), logger=Mock(), check_cancelled=Mock())
        dock = e["MarkerDock"](h, helper.config())
        dock._find_marker = lambda follower: None
        offsets = iter([-80] * 12 + [45] * 20 + [0] * 30)

        def sleep(dt):
            clock[0] += dt
            h.main_image = NS(gray=dock.target_quad + [next(offsets, 0), 0])

        h.sleep = sleep
        dock.detector = NS(detect_quad=lambda gray: gray)
        self.assertEqual(dock.run(), (0.23, 0))
        commands = [c.kwargs for c in h.mobility.send_cmd_vel.call_args_list]
        self.assertTrue(any(c["angular_z"] < -0.03 for c in commands))
        self.assertTrue(all(abs(c["linear_x"]) < 1e-9 for c in commands))
        self.assertTrue(any("overshoot recovery" in c.args[0] for c in h.logger.info.call_args_list))
