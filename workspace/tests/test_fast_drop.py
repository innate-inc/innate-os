import ast
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock


class Failed(Exception):
    pass


class FastDropTests(unittest.TestCase):
    def setUp(self):
        p = Path(__file__).parents[1] / "innate_skills/drop_in_box_fast.py"
        t = ast.parse(p.read_text())
        t.body = [n for n in t.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        env = dict(
            math=math,
            DropInBox=object,
            PARAMS=dict(carry_x=0.24, arm_pitch=1.3, drop_inset=0.08, drop_inset_min=0.03),
            SkillReturn=str,
            SkillFailed=Failed,
        )
        exec(compile(t, str(p), "exec"), env)
        self.s = env["DropInBoxFast"]()
        s = self.s
        self.events = []
        s.manipulation = Mock()
        s.manipulation.clamp_reach = lambda x, y: (x, y)
        s.manipulation.move_to.side_effect = self.move
        s.manipulation.gripper_open.side_effect = lambda **kw: self.events.append("open")
        s._lift_out = lambda: self.events.append("lift-clear")
        s.overlay = Mock()
        s.check_cancelled = Mock()
        s.sleep = Mock()
        s._over_rim = False
        s._released = False
        s.joint_states = NS(position=[0.1, 0.2, 0.3, 0.4, 0.0, 0.1])
        s.manipulation.move_joints.side_effect = lambda j, **kw: self.events.append(j[4])

    def move(self, x, y, z, **kw):
        self.events.append(z)
        return NS(x=x, y=y, z=z)

    def test_raise_reach_release_then_clear(self):
        self.s._release_at(0.23, 0)
        self.assertEqual(self.events, [0.28, 0.24, "open", -math.pi / 2, math.pi / 2, "lift-clear"])

    def test_shake_preserves_arm_and_open_grip(self):
        self.s._release_at(0.23, 0)
        for call in self.s.manipulation.move_joints.call_args_list:
            self.assertEqual(call.args[0][:4], [0.1, 0.2, 0.3, 0.4])
            self.assertEqual(len(call.args[0]), 5)
        self.assertAlmostEqual(self.s.manipulation.move_joints.call_args_list[1].kwargs["duration"], math.pi / 2)

    def test_missing_joints_does_not_shake_or_retreat(self):
        self.s.joint_states = None
        with self.assertRaises(Failed):
            self.s._release_at(0.23, 0)
        self.s.manipulation.move_joints.assert_not_called()
        self.assertTrue(self.s._released)
        self.assertTrue(self.s._over_rim)

    def test_cancel_during_shake_stops_second_rotation(self):
        self.s.manipulation.move_joints.side_effect = lambda *a, **kw: setattr(
            self.s.check_cancelled, "side_effect", Failed("stop")
        )
        with self.assertRaises(Failed):
            self.s._release_at(0.23, 0)
        self.assertEqual(self.s.manipulation.move_joints.call_count, 1)
        self.assertTrue(self.s._over_rim)

    def test_failed_raise_never_reaches_or_opens(self):
        self.s.manipulation.move_to.side_effect = lambda *a, **kw: NS(x=0.24, y=0, z=0.1)
        with self.assertRaises(Failed):
            self.s._release_at(0.23, 0)
        self.s.manipulation.gripper_open.assert_not_called()

    def test_inaccurate_release_pose_keeps_cleanup_latch(self):
        self.s.manipulation.move_to.side_effect = [NS(x=0.24, y=0, z=0.28), NS(x=0.25, y=0, z=0.24)]
        with self.assertRaises(Failed):
            self.s._release_at(0.23, 0)
        self.assertTrue(self.s._over_rim)
        self.s.manipulation.gripper_open.assert_not_called()

    def test_cancellation_before_open_preserves_grip(self):
        self.s.check_cancelled.side_effect = [None, Failed("stop")]
        with self.assertRaises(Failed):
            self.s._release_at(0.23, 0)
        self.s.manipulation.gripper_open.assert_not_called()
        self.assertFalse(self.s._released)


if __name__ == "__main__":
    unittest.main()
