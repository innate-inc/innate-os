"""Shortcut activation and fallback regressions; no physical robot commands."""

import ast
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).parents[1] / "innate_skills"


def functions(*names):
    tree = ast.parse((ROOT / "approach.py").read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    env = {"math": math, "vision": NS(LK_PARAMS={})}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "approach", "exec"), env)
    return env


class TrackAnchorTests(unittest.TestCase):
    def setUp(self):
        self.track = functions("track_floor_anchor")["track_floor_anchor"]
        self.grid = np.array(
            [[x, y] for x in [-24, -12, 0, 12, 24] for y in [-24, -12, 0, 12, 24]], dtype=np.float32
        ).reshape(-1, 1, 2)

    def run_track(self, status, back_error=0):
        nxt = self.grid + np.array([2.0, 1.0], dtype=np.float32)
        src = self.grid.reshape(-1, 2)[status.reshape(-1) == 1]
        back = (src + back_error).reshape(-1, 1, 2)
        cv = NS(calcOpticalFlowPyrLK=Mock(side_effect=[(nxt, status, None), (back, np.ones((len(src), 1)), None)]))
        with patch.dict(sys.modules, {"cv2": cv}):
            return self.track("before", "after", self.grid, (100, 200))

    def test_partial_feature_loss_keeps_original_contact_anchor(self):
        status = (self.grid.reshape(-1, 2)[:, 0] >= 0).astype(np.uint8).reshape(-1, 1)
        self.assertEqual(self.run_track(status), (102.0, 201.0))
        # The old destination-centre algorithm spuriously moves 12px sideways.
        self.assertEqual(
            float(np.median((self.grid + np.array([2, 1]))[status.flatten() == 1].reshape(-1, 2)[:, 0])), 14.0
        )

    def test_inconsistent_reverse_tracking_rejected(self):
        self.assertIsNone(self.run_track(np.ones((25, 1)), back_error=5))

    def test_too_few_features_rejected(self):
        status = np.zeros((25, 1))
        status[:7] = 1
        self.assertIsNone(self.run_track(status))


class ArrivalTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "approach.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FloorApproach")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "position_above")
        env = functions("tracked_arrival_ok")
        env.update(
            TRAVEL_MARGIN_M=0.08,
            PLATEAU_SLACK_M=0.04,
            SkillFailed=RuntimeError,
            base_to_odom=lambda o, xy: (o[0] + xy[0], xy[1]),
            odom_to_base=lambda o, xy: (xy[0] - o[0], xy[1]),
            floor_to_pixel=lambda x, y, t: (x * 1000, y * 1000),
            pixel_to_floor=lambda u, v, t: (u / 1000, v / 1000),
            inside_box=lambda px, u, v, hu, hv: abs(px[0] - u) <= hu and abs(px[1] - v) <= hv,
        )
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "arrival", "exec"), env)
        self.call = env["position_above"]
        self.s = NS(
            host=NS(overlay=Mock(), mobility=Mock(), main_image=True, logger=Mock(), name="drop"),
            p={"bearing_go_deg": 4, "tilt_deg": -12, "sweet_x": 0.23, "box_steps": 2, "accept_tracked_arrival": True},
            _sweet_box=lambda: ((230, 0), (10, 10), (6, 6)),
            _localize_retry=Mock(return_value=((0.23, 0), (230, 0))),
        )
        self.odom = (0, 0, 0)
        self.s.odom_xyt = lambda: self.odom

        def follow(*a, **kw):
            self.odom = (0.17, 0, 0)
            return "in_box", (230, 0)

        self.s._follow_into_box = follow

    def test_confirmed_arrival_skips_model(self):
        self.assertEqual(self.call(self.s, "box", (0.4, 0)), (0.23, 0))
        self.s._localize_retry.assert_not_called()
        self.assertIn("skipping model", self.s.host.logger.info.call_args.args[0])

    def test_odometry_disagreement_uses_model_and_logs_reason(self):
        self.call(self.s, "box", (0.5, 0))
        self.s._localize_retry.assert_called_once()
        self.assertTrue(any("confirmation required" in c.args[0] for c in self.s.host.logger.info.call_args_list))


class TrustedSockArrivalTests(ArrivalTests):
    def setUp(self):
        super().setUp()
        self.s.p.update(trust_sock_tracking=True, stable_arrival_margin=True, arrival_tracking_spread_m=0.02)
        self.s._arrival_samples = [(0.23, 0)] * 3

    def test_large_odom_disagreement_no_longer_calls_model(self):
        self.assertEqual(self.call(self.s, "sock", (0.55, 0)), (0.23, 0))
        self.s._localize_retry.assert_not_called()

    def test_sock_spread_up_to_two_cm_skips_model(self):
        self.s._arrival_samples = [(0.23, 0), (0.23, 0.02), (0.23, 0.01)]
        self.assertEqual(self.call(self.s, "sock", (0.55, 0)), (0.23, 0))
        self.s._localize_retry.assert_not_called()

    def test_fewer_than_three_fresh_samples_requires_confirmation(self):
        self.s._arrival_samples = [(0.23, 0)] * 2
        self.call(self.s, "sock", (0.55, 0))
        self.s._localize_retry.assert_called_once()

    def test_unstable_track_still_requires_confirmation(self):
        self.s._arrival_samples = [(0.23, 0), (0.251, 0), (0.23, 0)]
        self.call(self.s, "sock", (0.55, 0))
        self.s._localize_retry.assert_called_once()

    def test_odometry_disagreement_uses_model_and_logs_reason(self):
        # The inherited assertion applies to the default mode, not this opt-in.
        self.s.p["trust_sock_tracking"] = False
        super().test_odometry_disagreement_uses_model_and_logs_reason()

    def test_invalid_or_out_of_reach_track_never_accepted(self):
        gate = functions("tracked_arrival_ok")["tracked_arrival_ok"]
        for target in [None, (float("nan"), 0), (0.5, 0), (0.315, 0.1)]:
            self.assertFalse(gate(target, None, 0.315, stable=True, trust_tracking=True))
        self.assertTrue(gate((0.315, 0), None, 0.315, stable=True, trust_tracking=True))
        self.assertFalse(gate((0.315, 0), None, 0.315, stable=True))


class CarryTargetTests(unittest.TestCase):
    def test_fast_shared_pose_within_robot_joint_limits(self):
        tree = ast.parse((ROOT / "drop_in_box_fast.py").read_text())
        value = next(
            n.value
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "DEMO_CARRY_JOINTS" for t in n.targets)
        )
        target = ast.literal_eval(value)
        limits = [(-1.5708, 1.5708), (-1.5708, 1.22), (-1.5708, 1.7453), (-1.9199, 1.7453), (-1.5708, 1.5708)]
        for j, (low, high) in zip(target, limits, strict=False):
            self.assertTrue(low <= j <= high)


class ApproachSpeedTests(unittest.TestCase):
    def test_only_distant_aligned_targets_get_faster_translation(self):
        limits = functions("approach_linear_limits")["approach_linear_limits"]
        p = dict(
            fast_far_approach=True,
            sweet_x=0.23,
            box_half_px=50,
            follow_gain_lin=0.06,
            drive_v_min=0.04,
            drive_v_max=0.1,
        )
        self.assertEqual(limits(p, (0.5, 0), 30), (0.14, 0.08, 0.18))
        for xy, err in [((0.33, 0), 0), ((0.23, 0), 0), ((0.15, 0), 0), ((0.5, 0), 101)]:
            self.assertEqual(limits(p, xy, err), (0.06, 0.04, 0.1))
        self.assertEqual(limits({**p, "fast_far_approach": False}, (0.5, 0), 0), (0.06, 0.04, 0.1))
