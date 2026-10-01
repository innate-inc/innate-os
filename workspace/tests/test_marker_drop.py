import ast
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).parents[1] / "innate_skills"


class MarkerDropTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.dock = Mock()
        self.dock.run.side_effect = lambda: self.events.append("dock")
        self.retreat = Mock()
        self.retreat.drive.side_effect = lambda *a: self.events.append("retreat") or True
        base = type(
            "Base",
            (),
            {
                "_p": dict(
                    tilt_deg=-12,
                    drop_inset=0.08,
                    drop_inset_min=0.03,
                    carry_x=0.24,
                    arm_pitch=1.3,
                    travel_joints=[],
                    drive_kp=1,
                    drive_v_max=0.1,
                    drive_v_min=0.04,
                ),
                "RELEASE_Z": 0.24,
                "CLEARANCE_Z": 0.26,
            },
        )
        tree = ast.parse((ROOT / "drop_in_box_aruco.py").read_text())
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        self.config = dict(head_tilt_deg=-12, near_xy=[0.23, 0], image_size=[640, 480])
        env = dict(
            time=time,
            SkillReturn=str,
            SkillFailed=RuntimeError,
            DropInBoxFast=base,
            load_config=lambda: self.config,
            MarkerDock=lambda *a: self.dock,
            FloorApproach=lambda *a: self.retreat,
            fresh_sock_held=lambda h: True,
        )
        exec(compile(tree, "marker_drop", "exec"), env)
        self.s = env["DropInBoxAruco"]()
        s = self.s
        s.logger = Mock()
        s.manipulation = Mock()
        s.manipulation.clamp_reach = lambda x, y: (x, y)
        s.manipulation.reachable.return_value = True
        s.head = Mock()
        s.mobility = Mock()
        s.overlay = Mock()
        s.llm = Mock()
        s.wait_for = Mock()
        s._secure_grip = Mock(return_value=True)
        s._holding = Mock(return_value=True)
        s._carry_pose = Mock()
        s._detect_px = Mock()
        s._retract = Mock()
        s._release_at = Mock(side_effect=lambda *a: self.events.extend(["release", "lift-clear"]))

    def test_dock_release_clear_then_retreat_without_model(self):
        self.assertIn("not visually verified", self.s.execute())
        self.assertEqual(self.events, ["dock", "release", "lift-clear", "retreat"])
        self.s.llm.ask.assert_not_called()

    def test_right_offset_is_exact_and_preflighted_without_changing_docking(self):
        self.s.manipulation.clamp_reach = lambda x, y: (x, max(-0.1, min(0.1, y)))
        self.assertEqual(self.s._release_xy(0.23, 0), (0.31, -0.15))
        self.s.execute()
        self.assertEqual(self.config["near_xy"], [0.23, 0])
        self.s._release_at.assert_called_once_with(0.23, 0)
        for call in self.s.manipulation.reachable.call_args_list:
            self.assertEqual(call.args[1], -0.15)

    def test_lost_marker_cannot_release(self):
        self.dock.run.side_effect = RuntimeError("lost marker")
        with self.assertRaises(RuntimeError):
            self.s.execute()
        self.s._release_at.assert_not_called()
        self.retreat.drive.assert_not_called()
        self.s.mobility.stop.assert_called()

    def test_failed_clearance_never_retreats(self):
        self.s._release_at.side_effect = RuntimeError("clearance failed")
        with self.assertRaises(RuntimeError):
            self.s.execute()
        self.retreat.drive.assert_not_called()

    def test_unreachable_path_fails_before_driving_or_opening(self):
        self.s.manipulation.reachable.return_value = False
        with self.assertRaises(RuntimeError):
            self.s.execute()
        self.dock.run.assert_not_called()
        self.s._release_at.assert_not_called()
