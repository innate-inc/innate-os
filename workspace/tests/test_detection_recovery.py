import ast
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from test_shortcut_activation import ArrivalTests, functions

ROOT = Path(__file__).parents[1] / "innate_skills"


class RecoveryRoutingTests(ArrivalTests):
    def test_after_turn_visual_recovery_skips_model(self):
        self.s.p["local_visual_recovery"] = True
        self.s.rotate_by = Mock()
        self.s._recover_visual = Mock(return_value=((0.4, 0), (400, 0)))
        self.assertEqual(self.call(self.s, "sock", (0.4, 0.1)), (0.23, 0))
        self.s._recover_visual.assert_called_once()
        self.s._localize_retry.assert_not_called()

    def test_uncertain_turn_recovery_calls_detector(self):
        self.s.p["local_visual_recovery"] = True
        self.s.rotate_by = Mock()
        self.s._recover_visual = Mock(return_value=(None, None))
        self.call(self.s, "sock", (0.4, 0.1))
        self.s._localize_retry.assert_called_once()

    def test_marginal_agreement_requires_stable_fresh_samples(self):
        gate = functions("tracked_arrival_ok")["tracked_arrival_ok"]
        self.assertFalse(gate((0.353, -0.037), (0.395, -0.036), 0.315))
        self.assertTrue(gate((0.353, -0.037), (0.395, -0.036), 0.315, stable=True))
        self.assertFalse(gate((0.353, -0.037), (0.400, -0.036), 0.315, stable=True))
        self.assertFalse(gate((0.36, 0), (0.36, 0), 0.315, stable=True))


class RecoveryLoopTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "approach.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FloorApproach")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_recover_visual")
        self.clock = 0
        self.host = NS(mobility=Mock(), logger=Mock(), main_image=object(), name="test")

        def sleep(dt):
            self.clock += dt
            if self.fresh:
                self.host.main_image = object()

        self.host.sleep = sleep
        self.fresh = True
        env = dict(
            math=math,
            time=NS(monotonic=lambda: self.clock),
            vision=NS(b64_to_gray=lambda x: x),
            pixel_to_floor=lambda x, y, t: (x / 1000, y / 1000),
            floor_to_pixel=lambda x, y, t: (x * 1000, y * 1000),
            base_to_odom=lambda o, xy: xy if o is not None else None,
            odom_to_base=lambda o, xy: xy if o is not None else None,
        )
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "recovery", "exec"), env)
        self.run_recovery = env["_recover_visual"]
        self.s = NS(
            host=self.host,
            _local_tracker=NS(anchor=(300, 100), track=Mock(return_value=(300, 100))),
            _tracker_odom=(0, 0, 0),
            odom_xyt=lambda: (0, 0, 0),
            p={"tilt_deg": 0},
        )

    def test_requires_two_distinct_frames(self):
        self.assertEqual(self.run_recovery(self.s), ((0.3, 0.1), (300, 100)))
        self.assertEqual(self.s._local_tracker.track.call_count, 2)
        self.host.mobility.send_cmd_vel.assert_not_called()

    def test_stale_camera_and_missing_odom_fall_back(self):
        self.fresh = False
        self.assertEqual(self.run_recovery(self.s), (None, None))
        self.assertLessEqual(self.clock, 0.48)
        self.assertIsNone(self.s._local_tracker)
        self.setUp()
        self.s.odom_xyt = lambda: None
        self.assertEqual(self.run_recovery(self.s), (None, None))

    def test_large_disagreement_and_jitter_fall_back(self):
        for positions in [[(360, 100)] * 20, [(300, 100), (310, 100)] * 10]:
            self.setUp()
            self.s._local_tracker.track.side_effect = positions
            self.assertEqual(self.run_recovery(self.s), (None, None))
            self.host.mobility.send_cmd_vel.assert_not_called()

    def test_stop_is_cancellable(self):
        self.host.sleep = Mock(side_effect=RuntimeError("cancelled"))
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.run_recovery(self.s)
        self.host.mobility.stop.assert_called_once()


class FollowRecoveryTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "approach.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FloorApproach")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_follow_into_box")
        self.clock = 0
        self.host = NS(main_image=object(), mobility=Mock(), overlay=Mock(), logger=Mock(), name="sock")

        def sleep(dt):
            self.clock += dt
            self.host.main_image = object()

        self.host.sleep = sleep
        env = dict(
            math=math,
            time=NS(monotonic=lambda: self.clock),
            FOLLOW_TIMEOUT_S=1,
            vision=NS(b64_to_gray=lambda x: x, grid_pts=lambda *p: None),
            pixel_to_floor=lambda u, v, t: (u / 1000, v / 1000),
            IMG_W=640,
            IMG_H=480,
            inside_box=lambda *args: True,
        )
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "follow", "exec"), env)
        self.follow = env["_follow_into_box"]
        self.tracker = NS(track=Mock(return_value=(315, 10)), gray="recovered")
        self.s = NS(
            host=self.host,
            p={"local_visual_recovery": True, "tilt_deg": 0},
            _local_tracker=self.tracker,
            odom_xyt=lambda: (0, 0, 0),
            _sweet_box=lambda: ((315, 10), (20, 20), (10, 10)),
            _draw_track=Mock(),
            _recover_visual=Mock(return_value=((0.315, 0.01), (315, 10))),
        )

    def test_transient_loss_recovers_then_collects_three_arrival_frames(self):
        self.tracker.track.side_effect = [None, (315, 10), (315, 10)]
        self.assertEqual(self.follow(self.s, (315, 10)), ("in_box", (315, 10)))
        self.s._recover_visual.assert_called_once()
        self.assertEqual(len(self.s._arrival_samples), 3)
        self.host.mobility.send_cmd_vel.assert_not_called()

    def test_failed_recovery_stops_and_returns_to_detection(self):
        self.tracker.track.return_value = None
        self.s._recover_visual.return_value = (None, None)
        self.assertEqual(self.follow(self.s, (315, 10)), ("lost", None))
        self.host.mobility.stop.assert_called()

    def test_odom_loss_stops_without_visual_guess(self):
        self.s.odom_xyt = lambda: None
        self.assertEqual(self.follow(self.s, (315, 10)), ("lost", None))
        self.tracker.track.assert_not_called()
        self.host.mobility.stop.assert_called()


class SockPromptTests(unittest.TestCase):
    def test_first_detection_is_short_reacquisition_preserves_identity(self):
        tree = ast.parse((ROOT / "pick_sock_fast.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        ask = Mock(return_value=("reply", "image"))
        candidate = (100, 200, None, (80, 180, 120, 220))
        env = dict(ask_head=ask, vision=NS(parse_det_cands_boxed=lambda text: [candidate]))
        methods = [
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name in ("_detect_px", "_detect_candidates", "_detection_question", "_parse_detections")
        ]
        exec(compile(ast.Module(body=methods, type_ignores=[]), "sock", "exec"), env)
        s = NS(
            _last_seen=None,
            _p={"settle_s": 0.6, "close_strength": 0.6},
            overlay=Mock(),
            _choose_cand=Mock(return_value=candidate),
            _sighting=lambda c: (1, 2, 0.3),
            _draw_sighting=Mock(),
        )
        s._detect_candidates = lambda prompt: env["_detect_candidates"](s, prompt)
        s._detection_question = lambda selection: env["_detection_question"](s, selection)
        s._parse_detections = lambda text: env["_parse_detections"](s, text)
        self.assertEqual(env["_detect_px"](s, "sock"), (100, 200))
        self.assertIn("one best match", ask.call_args.args[1])
        self.assertNotIn("grip_strength", ask.call_args.args[1])
        self.assertEqual(s._grip_strength, 0.6)
        env["_detect_px"](s, "sock")
        self.assertIn("all matching socks", ask.call_args.args[1])
        s._choose_cand.return_value = None
        self.assertIsNone(env["_detect_px"](s, "sock"))
        self.assertIsNone(s._local_detection_box)
