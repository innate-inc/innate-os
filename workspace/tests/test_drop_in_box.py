"""Exercise drop control flow without ROS or physical motion."""

import ast
import math
import re
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock


class Failure(Exception):
    pass


class Cancelled(Exception):
    pass


def load_skill():
    # Load the actual class and helpers, replacing only ROS/SDK dependencies.
    tree = ast.parse((Path(__file__).parents[1] / "innate_skills/drop_in_box.py").read_text())
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    env = dict(
        math=math,
        re=re,
        APPROACH_PARAMS={},
        ARM_ORIGIN=(0, 0, 0),
        IMG_H=480,
        IMG_W=640,
        Skill=object,
        SkillReturn=str,
        FloorApproach=object,
        Llm=lambda *a, **k: NS(available=True, model=a[0], thinking=k.get("thinking")),
        ArmFailed=Failure,
        ArmUnhealthy=Failure,
        SkillFailed=Failure,
    )
    for name in ("Head", "JointStates", "MainImage", "Manipulation", "Mobility", "Odometry", "WristImage"):
        env[name] = type(name, (), {})
    exec(compile(tree, "drop_in_box.py", "exec"), env)
    return env


class DropTests(unittest.TestCase):
    def setUp(self):
        self.env = load_skill()
        self.s = self.env["DropInBox"]()
        self.events = []
        s = self.s
        s.logger = Mock()
        s.overlay = Mock()
        s.head = Mock()
        s.mobility = Mock()
        s.joint_states = NS(position=[0] * 5 + [0.2])
        s.main_image, s.wrist_image = "head-before-fold", "wrist-before-fold"
        s.check_cancelled = Mock()
        s.sleep = Mock()
        s.wait_for = Mock()
        s.say = Mock()
        s.fail = lambda msg: (_ for _ in ()).throw(Failure(msg))
        s.manipulation = Mock(REST=[0] * 6)
        s.manipulation.pose = NS(z=0.1)
        s.manipulation.clamp_reach.side_effect = lambda x, y: (x, y)
        s.manipulation.move_by.side_effect = self.lift
        s.manipulation.gripper_open.side_effect = lambda **k: self.events.append("open")
        s.manipulation.move_joints.side_effect = self.fold
        s.manipulation.wait.side_effect = lambda: self.events.append("join")
        s._secure_grip = lambda: True
        s._holding = lambda closed: True
        s._carry_pose = lambda joints: None
        s._lift_clear = lambda y: None
        s._release_x = lambda x, z: x
        s.llm = NS(available=True, ask=self.ask)
        self.approach = NS(
            search=lambda p: (0.2, 0), position_above=lambda p, xy: xy, drive=lambda d: self.events.append("back")
        )
        self.env["FloorApproach"] = lambda *a: self.approach
        self.env["settled_frame"] = lambda *a: s.main_image

    def lift(self, **kw):
        self.events.append("lift")
        return NS(z=self.s._clearance_z)

    def fold(self, *a, **kw):
        self.events.append("fold" if kw.get("block", True) else "fold-start")
        self.s.wrist_image = "wrist-after-fold"

    def ask(self, images, *a, **kw):
        self.events.append("vision")
        self.assertEqual(images, ["head-before-fold", "wrist-before-fold"])
        return "NO"

    def test_full_success_order_and_single_fold(self):
        self.s.execute()
        self.assertEqual(self.events, ["open", "lift", "back", "fold-start", "vision", "join"])

    def test_stalled_lift_never_drives_or_folds(self):
        self.s.manipulation.move_by.side_effect = lambda **k: NS(z=0.01)
        with self.assertRaises(Failure):
            self.s.execute()
        self.assertNotIn("back", self.events)
        self.assertNotIn("fold", self.events)
        self.assertTrue(self.s._over_rim)

    def test_lift_exception_never_drives(self):
        self.s.manipulation.move_by.side_effect = Failure("servo")
        with self.assertRaises(Failure):
            self.s.execute()
        self.assertNotIn("back", self.events)
        self.assertNotIn("fold", self.events)

    def test_cancel_after_release_only_cleans_up(self):
        self.s.sleep.side_effect = Cancelled()
        with self.assertRaises(Cancelled):
            self.s.execute()
        self.assertEqual(self.events, ["open", "lift", "fold"])

    def test_cancel_during_vision_joins_fold(self):
        self.s.llm.ask = Mock(side_effect=Cancelled())
        with self.assertRaises(Cancelled):
            self.s.execute()
        self.assertEqual(self.events[-1], "join")
        self.assertNotIn("fold", self.events)

    def test_failed_async_fold_retries_in_cleanup(self):
        self.s.manipulation.wait.side_effect = Failure("motion")
        self.s.execute()
        self.assertEqual(self.events.count("fold"), 1)

    def test_missing_images_keeps_blocking_cleanup(self):
        self.s.main_image = self.s.wrist_image = None
        self.s._j6 = lambda: 0
        self.s.execute()
        self.assertEqual(self.events, ["open", "lift", "back", "fold"])

    def test_missed_drop_still_joins_fold(self):
        self.s.llm.ask = lambda *a, **k: "YES"
        with self.assertRaises(Failure):
            self.s.execute()
        self.assertEqual(self.events[-1], "join")

    def test_nonfinite_clearance_rejected(self):
        self.s._clearance_z = 0.2
        self.s._over_rim = True
        self.s.manipulation.move_by.side_effect = lambda **k: NS(z=float("nan"))
        with self.assertRaises(Failure):
            self.s._lift_out()
        self.assertTrue(self.s._over_rim)


if __name__ == "__main__":
    unittest.main()
