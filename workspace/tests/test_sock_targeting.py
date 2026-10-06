import ast
import math
import re
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(__file__).parents[1] / "innate_skills"


class TargetingTests(unittest.TestCase):
    def test_tracking_gate(self):
        tree = ast.parse((ROOT / "approach.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tracked_arrival_ok")
        env = {"math": math}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "gate", "exec"), env)
        gate = env["tracked_arrival_ok"]
        self.assertTrue(gate((0.315, 0.01), (0.32, 0.01), 0.315))
        for tracked, odom in [
            (None, (0.315, 0)),
            ((0.315, 0), None),
            ((0.315, 0), (0.5, 0)),
            ((0.5, 0), (0.5, 0)),
            ((0.315, 0.1), (0.315, 0.1)),
            ((float("nan"), 0), (0.315, 0)),
        ]:
            self.assertFalse(gate(tracked, odom, 0.315))

    def verify(self, reply, in_place=True, j6=0.2):
        tree = ast.parse((ROOT / "pick_any_object.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickAnyObject")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_grasp_verified")
        env = dict(
            FloorApproach=object, VERIFY_BACKUP_M=0.15, GRIPPER_EMPTY_J6=-0.085, re=re, settled_frame=lambda *a: "head"
        )
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "verify", "exec"), env)
        s = NS(
            _p={"verify_in_place": in_place, "settle_s": 0.6},
            overlay=Mock(),
            mobility=Mock(),
            joint_states=NS(position=[0] * 5 + [j6]),
            wrist_image="wrist",
            _fold_to_carry=Mock(),
            _join_fold=Mock(),
            logger=Mock(),
            llm=Mock(),
        )
        s.llm.ask.return_value = reply
        approach = Mock()
        result = env["_grasp_verified"](s, "sock", approach)
        return result, s, approach

    def test_held_sock_verified_without_any_base_drive(self):
        held, s, approach = self.verify("YES")
        self.assertTrue(held)
        approach.drive.assert_not_called()
        s.mobility.stop.assert_called_once()
        self.assertIn("visibly held", s.llm.ask.call_args.args[1])
        s._join_fold.assert_called_once()

    def test_empty_or_ambiguous_does_not_claim_success(self):
        for reply, j6 in [("NO", 0.2), (None, 0.2), ("YES or NO", 0.2), ("YES", -0.085)]:
            held, s, approach = self.verify(reply, j6=j6)
            self.assertFalse(held)
            approach.drive.assert_not_called()

    def test_generic_pickup_preserves_existing_verification(self):
        held, s, approach = self.verify("NO", in_place=False)
        self.assertTrue(held)
        approach.drive.assert_called_once_with(-0.15, brisk=True)


if __name__ == "__main__":
    unittest.main()
