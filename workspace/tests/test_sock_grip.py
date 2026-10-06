import ast
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(__file__).parents[1] / "innate_skills"


class SockGripTests(unittest.TestCase):
    def setUp(self):
        self.t = 0
        tree = ast.parse((ROOT / "sock_grip.py").read_text())
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        self.env = dict(math=math, time=NS(monotonic=lambda: self.t), SkillFailed=RuntimeError)
        exec(compile(tree, "sock_grip", "exec"), self.env)
        self.h = NS(joint_states=NS(position=[0] * 6), manipulation=NS(_grip_target=-0.6), logger=Mock())

    def read(self, values):
        it = iter(values)

        def sleep(dt):
            self.t += dt
            value = next(it, None)
            if value is not None:
                self.h.joint_states = NS(position=[0] * 5 + [value])

        self.h.sleep = sleep
        return self.env["fresh_sock_held"](self.h)

    def test_thin_sock_accepted_and_measured_empty_rejected(self):
        self.assertTrue(self.read([0.029, 0.028]))
        for j6 in [-0.085, -0.064, -0.040, -0.03]:
            self.setUp()
            self.assertFalse(self.read([j6, j6]))

    def test_open_stale_nan_and_threshold_jitter_are_unknown(self):
        for values in [[], [float("nan")] * 30, [0.9] * 30, [-0.035, -0.025] * 15]:
            self.setUp()
            with self.assertRaises(RuntimeError):
                self.read(values)
        self.setUp()
        self.h.manipulation._grip_target = 0.5
        with self.assertRaises(RuntimeError):
            self.read([0.2, 0.2])

    def test_cancel_propagates(self):
        self.h.sleep = Mock(side_effect=RuntimeError("cancel"))
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            self.env["fresh_sock_held"](self.h)


class SockRetryTests(unittest.TestCase):
    def setUp(self):
        self.closed = Mock()
        base = type("PickBase", (), {"_close_twist_lift": self.closed})
        self.fresh = Mock()
        tree = ast.parse((ROOT / "pick_sock_fast.py").read_text())
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        env = dict(
            PickAnyObject=base,
            PARAMS={},
            DEMO_CARRY_JOINTS=[],
            SkillReturn=str,
            SkillFailed=RuntimeError,
            fresh_sock_held=self.fresh,
        )
        exec(compile(tree, "pick_sock_fast", "exec"), env)
        self.s = env["PickSockFast"]()
        for name in ["check_cancelled", "_claw_open", "_push_to_floor", "_fold_to_carry", "_join_fold"]:
            setattr(self.s, name, Mock())
        self.s.manipulation = NS(pose=NS(z=0.15))
        self.s.overlay = Mock()
        self.s.mobility = Mock()
        self.s.llm = Mock()

    def test_empty_then_held_retries_before_any_base_movement(self):
        self.fresh.side_effect = [False, True]
        self.s._close_twist_lift(0.3, 0, 0, 0, 0)
        self.assertEqual(self.closed.call_count, 2)
        self.s._claw_open.assert_called_once()
        self.s._push_to_floor.assert_called_once()
        self.s.mobility.send_cmd_vel.assert_not_called()
        self.s.llm.ask.assert_not_called()

    def test_empty_bounded_at_three_unknown_never_retries(self):
        self.fresh.return_value = False
        with self.assertRaisesRegex(RuntimeError, "three attempts"):
            self.s._close_twist_lift(0.3, 0, 0, 0, 0)
        self.assertEqual(self.closed.call_count, 3)
        self.setUp()
        self.fresh.side_effect = RuntimeError("unknown feedback")
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.s._close_twist_lift(0.3, 0, 0, 0, 0)
        self.assertEqual(self.closed.call_count, 1)
        self.s._claw_open.assert_not_called()

    def test_carry_check_uses_no_picture(self):
        self.fresh.return_value = True
        self.assertTrue(self.s._grasp_verified("sock", Mock()))
        self.s._join_fold.assert_called_once()
        self.s.llm.ask.assert_not_called()
