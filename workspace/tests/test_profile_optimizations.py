"""Regression coverage for the fast sock sequence, without moving hardware."""

import ast
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from test_drop_in_box import DropTests, Failure
from test_sock_demo import load

ROOT = Path(__file__).parents[1] / "innate_skills"


class PickupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.grasp = Mock()
        base = type("Base", (), {"_grasp_at": self.grasp})
        self.odom = (0.0, 0.0, 0.0)
        self.approach = Mock()
        self.approach.odom_xyt.side_effect = lambda: self.odom

        def drive(d):
            self.odom = (d, 0.0, 0.0)
            return True

        self.approach.drive.side_effect = drive
        env = dict(
            PickAnyObject=base,
            PARAMS={"grasp_x_off": 0.05, "hover_z": 0.15, "arm_pitch": 1.3},
            SkillReturn=str,
            SkillFailed=Failure,
            DEMO_CARRY_JOINTS=[0] * 5,
            FloorApproach=lambda *a: self.approach,
            base_to_odom=lambda o, xy: None if o is None else (o[0] + xy[0], xy[1]),
            odom_to_base=lambda o, xy: None if o is None else (xy[0] - o[0], xy[1]),
        )
        self.s = load(ROOT / "pick_sock_fast.py", env)["PickSockFast"]()
        self.s.manipulation = Mock()
        self.s.manipulation.clamp_reach.side_effect = lambda x, y: (x, y)
        self.s.manipulation.reachable.side_effect = lambda x, y, z, **kw: x <= 0.26
        self.s.check_cancelled = Mock()
        self.s._detect_px = Mock()

    def test_corrects_before_grasp_and_uses_measured_position(self):
        self.s._grasp_at("sock", (0.35, 0))
        self.approach.drive.assert_called_once_with(0.04)
        self.grasp.assert_called_once()
        self.assertAlmostEqual(self.grasp.call_args.args[1][0], 0.31)

    def test_reachable_target_never_moves_base(self):
        self.s._grasp_at("sock", (0.30, 0))
        self.approach.drive.assert_not_called()
        self.grasp.assert_called_once_with("sock", (0.30, 0))

    def test_no_odom_or_no_solution_never_moves(self):
        for odom in (None, (0.0, 0.0, 0.0)):
            self.odom = odom
            self.s.manipulation.reachable.return_value = False
            self.s.manipulation.reachable.side_effect = None
            with self.assertRaises(Failure):
                self.s._grasp_at("sock", (0.35, 0))
        self.approach.drive.assert_not_called()
        self.grasp.assert_not_called()

    def test_failed_drive_or_still_unreachable_never_lowers(self):
        for result in (False, True):
            self.approach.drive.side_effect = None
            self.approach.drive.return_value = result
            with self.assertRaises(Failure):
                self.s._grasp_at("sock", (0.35, 0))
        self.grasp.assert_not_called()

    def test_cancel_before_correction(self):
        self.s.check_cancelled.side_effect = Failure("stop")
        with self.assertRaises(Failure):
            self.s._grasp_at("sock", (0.35, 0))
        self.approach.drive.assert_not_called()


class DropShortcutsTests(unittest.TestCase):
    def setUp(self):
        self.secure = Mock(return_value=True)
        self.holding = Mock(return_value=False)
        self.carry = Mock()
        base = type("Base", (), {"_secure_grip": self.secure, "_holding": self.holding, "_carry_pose": self.carry})
        self.fresh = Mock(return_value=True)
        env = dict(
            math=math,
            DropInBox=base,
            PARAMS={"carry_grip": 0.6},
            SkillReturn=str,
            fresh_sock_held=self.fresh,
            closing_command=lambda m: (
                isinstance(m._grip_target, (int, float)) and math.isfinite(m._grip_target) and m._grip_target <= -0.3
            ),
        )
        self.s = load(ROOT / "drop_in_box_fast.py", env)["DropInBoxFast"]()
        self.s.manipulation = Mock(_grip_target=-0.6)
        self.s.logger = Mock()
        self.s._j6 = Mock(return_value=0.1)

    def test_loaded_closing_gripper_skips_reclose_and_vision(self):
        self.assertTrue(self.s._holding(self.s._secure_grip()))
        self.secure.assert_not_called()
        self.holding.assert_not_called()

    def test_unknown_or_open_command_requires_close(self):
        for target in (None, 0.5, float("nan")):
            self.s.manipulation._grip_target = target
            self.s._secure_grip()
        self.assertEqual(self.secure.call_count, 3)

    def test_empty_or_failed_close_never_uses_vision(self):
        self.fresh.return_value = False
        self.assertFalse(self.s._holding(True))
        self.fresh.assert_called_once_with(self.s)
        self.fresh.reset_mock()
        self.assertFalse(self.s._holding(False))
        self.fresh.assert_not_called()
        self.holding.assert_not_called()

    def test_matching_carry_skips_but_missing_or_wrong_pose_moves(self):
        self.s.joint_states = NS(position=[0] * 6)
        self.s._carry_pose([0] * 5)
        self.carry.assert_not_called()
        self.s._carry_pose([1] * 5)
        self.s.joint_states = None
        self.s._carry_pose([0] * 5)
        self.assertEqual(self.carry.call_count, 2)


class RetreatTests(DropTests):
    def test_retreat_scales_all_speed_terms_after_clearance(self):
        self.s._p = {**self.s._p, "retreat_speed_scale": 3.0, "drive_kp": 0.3, "drive_v_min": 0.04, "drive_v_max": 0.1}
        new = []

        def approach(host, params, detect):
            new.append(params)
            return self.approach

        self.env["FloorApproach"] = approach
        self.s.execute()
        self.assertEqual(self.events, ["open", "lift", "back", "fold-start", "vision", "join"])
        self.assertAlmostEqual(new[-1]["drive_v_min"], 0.12)
        self.assertAlmostEqual(new[-1]["drive_v_max"], 0.30)
        self.assertAlmostEqual(new[-1]["drive_kp"], 0.90)
        self.assertEqual(new[0]["drive_v_max"], 0.1)

    def test_failed_retreat_does_not_report_verified_drop(self):
        self.approach.drive = lambda d: False
        with self.assertRaises(Failure):
            self.s.execute()
        self.assertNotIn("vision", self.events)


if __name__ == "__main__":
    unittest.main()


class PickupMotionTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "pick_any_object.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickAnyObject")
        methods = [
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef)
            and n.name in ("_fold_to_carry", "_join_fold", "_rest_arm", "_lift_before_close")
        ]
        env = dict(ArmFailed=Failure, CARRY_ARM=[0] * 5, FOLD_S=1.2, time=NS(sleep=Mock()))
        exec(compile(ast.Module(body=methods, type_ignores=[]), "pickup", "exec"), env)
        self.s = type("Pick", (), {n.name: env[n.name] for n in methods})()
        self.s._p = {
            "close_strength": 0.6,
            "skip_carry_repeat": True,
            "close_lift_m": 0.01,
            "close_lift_s": 1.0,
            "close_lift_min_m": 0.004,
            "close_lift_tolerance_m": 0.0005,
        }
        self.s.manipulation = Mock(REST=[0] * 6)
        self.s.manipulation.pose = NS(z=0.03)
        self.s.logger = Mock()
        self.s._carried = False
        self.s._rung_pitch = Mock(return_value=1.3)

    def test_completed_fold_not_repeated(self):
        self.s._fold_to_carry()
        self.s._join_fold()
        self.s._rest_arm(True)
        self.assertEqual(self.s.manipulation.move_joints.call_count, 1)

    def test_rejected_fold_is_not_marked_carried(self):
        self.s.manipulation.move_joints.side_effect = [Failure("reject"), None, None]
        self.s._fold_to_carry()
        self.s._join_fold()
        self.s._rest_arm(True)
        self.assertFalse(self.s._carried)
        self.s.manipulation.wait.assert_not_called()
        self.assertEqual(self.s.manipulation.move_joints.call_count, 3)

    def test_preclose_lift_avoids_boundary_repeat_but_retries_real_shortfall(self):
        for rise, count in ((0.0038, 1), (0.002, 2)):
            self.s.manipulation.move_to.reset_mock()
            self.s.manipulation.move_to.return_value = NS(z=0.03 + rise)
            self.s._lift_before_close(0.25, 0, 0, 1.3, 0)
            self.assertEqual(self.s.manipulation.move_to.call_count, count)
