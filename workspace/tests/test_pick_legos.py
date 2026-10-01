"""LEGO post-lift retries through the inherited close/lift and execute paths."""

import ast
import inspect
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock


class Failed(Exception):
    pass


class Cancelled(Exception):
    pass


class ArmFailed(Exception):
    pass


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, t):
        self.now += t


class LegoTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1] / "innate_skills"
        tree = ast.parse((root / "pick_any_object.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickAnyObject")
        names = ("_close_grip", "_close_twist_lift", "_fingers_still", "execute")
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.clock = Clock()
        ns = dict(
            ArmFailed=ArmFailed,
            ArmUnhealthy=ArmFailed,
            SkillFailed=Failed,
            SkillReturn=str,
            time=self.clock,
            NAV_ARM=[],
            IMG_W=640,
            IMG_H=480,
        )
        exec(compile(ast.Module(body=methods, type_ignores=[]), "base_pickup", "exec"), ns)
        self.base = type("Base", (), {name: ns[name] for name in names})
        env = dict(
            math=math,
            time=self.clock,
            PickAnyObject=self.base,
            PARAMS=dict(
                close_strength=0.4,
                close_s=1,
                close_settle_max_s=0.8,
                lift_rad=0.6,
                lift_s=1.5,
                arm_pitch=1.3,
                tilt_deg=-20,
            ),
            SkillReturn=str,
            SkillFailed=Failed,
        )
        t = ast.parse((root / "pick_legos.py").read_text())
        t.body = [n for n in t.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        exec(compile(t, "pick_legos.py", "exec"), env)
        self.events = []
        self.samples = lambda: NS(position=[0] * 5 + [-0.06442719309119693])
        klass = type("TestLego", (env["PickLegos"],), {"joint_states": property(lambda _: self.samples())})
        self.s = klass()
        self.s.overlay = Mock()
        self.s.logger = Mock()
        self.s.manipulation = Mock()
        self.s.manipulation.pose = NS(z=0.22)
        self.s.manipulation.gripper_close.side_effect = lambda *a, **k: self.events.append("close")
        self.s.manipulation.move_joints.side_effect = lambda *a, **k: self.events.append("lift")
        self.s._lift_before_close = Mock()
        self.s._claw_open = Mock(side_effect=lambda: self.events.append("open"))
        self.s._push_to_floor = Mock(side_effect=lambda *a: self.events.append("lower"))
        self.s._arm_joints = lambda: [0] * 5 + [0.2]
        self.s.check_cancelled = Mock()
        self.s.sleep = self.clock.sleep
        self.s._holding = False
        self.s.llm = NS(available=True)
        self.s.head = Mock()
        self.s.mobility = Mock()
        self.s.say = Mock()
        self.s._stages = lambda: []
        self.s._detect_px = Mock()
        self.s._grasp_at = lambda *a: self.run_grasp()
        self.s._grasp_verified = Mock(side_effect=lambda *a: self.events.append("back-and-verify") or True)
        self.s._rest_arm = Mock()
        self.s.fail = lambda msg: (_ for _ in ()).throw(Failed(msg))
        ns["FloorApproach"] = lambda *a: NS(search=lambda p: (0.25, 0), position_above=lambda p, xy: xy)

    def run_grasp(self):
        self.s._close_twist_lift(0.25, 0, 0, 1.3, 0)

    def test_observed_empty_reading_retries_after_every_lift_before_backup(self):
        with self.assertRaises(Failed):
            self.s.execute()
        self.assertEqual(
            self.events[1:], ["close", "lift", "open", "lower", "close", "lift", "open", "lower", "close", "lift"]
        )
        self.s._grasp_verified.assert_not_called()
        self.assertFalse(self.s._holding)

    def test_second_attempt_holds_then_allows_verification(self):
        self.samples = lambda: NS(
            position=[0] * 5 + ([-0.0644] if self.s.manipulation.gripper_close.call_count < 2 else [0.2])
        )
        self.s.execute()
        self.assertEqual(self.events[-1], "back-and-verify")
        self.assertEqual(self.s._claw_open.call_count, 1)
        self.assertTrue(self.s._holding)

    def test_stale_empty_does_not_reopen_or_back_up(self):
        fixed = NS(position=[0] * 5 + [-0.0644])
        self.samples = lambda: fixed
        with self.assertRaises(Failed):
            self.s.execute()
        self.s._claw_open.assert_not_called()
        self.s._grasp_verified.assert_not_called()
        self.assertTrue(self.s._holding)

    def test_missing_feedback_does_not_reopen(self):
        self.samples = lambda: None
        with self.assertRaises(Failed):
            self.run_grasp()
        self.s._claw_open.assert_not_called()

    def test_nonfinite_feedback_does_not_reopen(self):
        self.samples = lambda: NS(position=[0] * 5 + [float("nan")])
        with self.assertRaises(Failed):
            self.run_grasp()
        self.s._claw_open.assert_not_called()

    def test_cancel_after_empty_prevents_retry(self):
        self.s.check_cancelled.side_effect = [None, Cancelled()]
        with self.assertRaises(Cancelled):
            self.run_grasp()
        self.assertEqual(self.events, ["close", "lift"])
        self.s._claw_open.assert_not_called()

    def test_close_hardware_failure_is_not_retried(self):
        self.s.manipulation.gripper_close.side_effect = ArmFailed()
        with self.assertRaises(ArmFailed):
            self.run_grasp()
        self.s._claw_open.assert_not_called()

    def test_nonempty_grasp_lifts_without_retry(self):
        self.samples = lambda: NS(position=[0] * 5 + [0.2])
        self.run_grasp()
        self.assertEqual(self.events, ["close", "lift"])

    def test_other_recorded_empty_value(self):
        self.samples = lambda: NS(position=[0] * 5 + [-0.0398835])
        self.assertTrue(self.s._claw_empty_after_lift())

    def test_fixed_target_has_no_parameters(self):
        self.assertEqual(list(inspect.signature(self.s.execute).parameters), [])
        self.samples = lambda: NS(position=[0] * 5 + [0.2])
        self.s.execute()
        self.assertIn("exclude the box itself and any LEGOs inside it", self.s._grasp_verified.call_args.args[0])

    def test_threshold_boundary(self):
        for value, expected in [(-0.03, True), (-0.029, False)]:
            self.samples = lambda value=value: NS(position=[0] * 5 + [value])
            self.assertEqual(self.s._claw_empty_after_lift(), expected)


if __name__ == "__main__":
    unittest.main()
