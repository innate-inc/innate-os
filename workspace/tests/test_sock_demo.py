"""Sock fast-path sequence and restricted agent surface, without ROS motion."""

import ast
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(__file__).parents[1]


def load(path, env):
    tree = ast.parse(path.read_text())
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, str(path), "exec"), env)
    return env


class SockDemoTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        tree = ast.parse((ROOT / "innate_skills/pick_any_object.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickAnyObject")
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ("_grasp_at", "_push_to_floor")]
        env = dict(
            math=math,
            Waypoint=lambda *a, **kw: NS(x=a[0], y=a[1], z=a[2], **kw),
            RUNG_MIN_M=0.01,
            ArmFailed=RuntimeError,
            ArmUnhealthy=RuntimeError,
        )
        exec(compile(ast.Module(body=methods, type_ignores=[]), "pickup", "exec"), env)
        base = type("Base", (), {n.name: env[n.name] for n in methods})
        params = dict(grasp_x_off=0.05, hover_z=0.15, hover_s=2.0, arm_pitch=1.3, floor_z=0.03, descend_abort_z=0.12)
        self.cls = load(
            ROOT / "innate_skills/pick_sock_fast.py",
            dict(
                PickAnyObject=base,
                PARAMS=params,
                SkillReturn=str,
                DEMO_CARRY_JOINTS=[0] * 5,
                math=math,
                Waypoint=env["Waypoint"],
                ArmUnhealthy=RuntimeError,
                ArmFailed=RuntimeError,
            ),
        )["PickSockFast"]
        s = self.s = self.cls()
        s.manipulation = Mock(GRIPPER_OPEN=1.0)
        s.manipulation.pose = NS(x=0.265, y=0, z=0.08)
        s.manipulation.clamp_reach.side_effect = lambda x, y: (x, y)
        s._aim = Mock()
        s.overlay = Mock()
        s.logger = Mock()
        s.check_cancelled = Mock()
        s._claw_open = Mock()
        s._wrist_descend = Mock(side_effect=AssertionError("wrist path used"))
        s._grasp_orientation = lambda x, y, r: (0, 1.3, 0)
        s._rung_pitch = lambda *a: 1.3
        s._close_twist_lift = Mock()

    def test_one_direct_descent_no_wrist_retargeting(self):
        self.s._grasp_at("sock", (0.315, 0))
        self.s._wrist_descend.assert_not_called()
        waypoints = self.s.manipulation.follow.call_args.args[0]
        self.assertEqual(len(waypoints), 1)
        self.assertEqual(waypoints[0].z, 0.03)
        self.assertEqual(waypoints[0].duration, 0.625)
        sweep = self.s.manipulation.follow.call_args_list[0].args[0]
        self.assertEqual([w.z for w in sweep], [0.15, 0.08])
        self.s.manipulation.move_to.assert_not_called()
        self.s._close_twist_lift.assert_called_once()
        self.assertTrue(self.s._soft_object)

    def test_cancel_before_descent_prevents_close(self):
        self.s.check_cancelled.side_effect = RuntimeError("stop")
        with self.assertRaises(RuntimeError):
            self.s._grasp_at("sock", (0.315, 0))
        self.s.manipulation.follow.assert_not_called()
        self.s._close_twist_lift.assert_not_called()

    def test_arm_remaining_high_prevents_close(self):
        self.s.manipulation.pose = NS(x=0.265, y=0, z=0.2)
        with self.assertRaises(RuntimeError):
            self.s._grasp_at("sock", (0.315, 0))
        self.s._close_twist_lift.assert_not_called()

    def test_close_twist_keeps_arm_fixed_and_preloads_before_send(self):
        s = self.s
        s._arm_joints = lambda: [0, 0.2, 0.3, 0.4, 1.2, 1.0]
        s._p = {**s._p, "close_strength": 0.6, "twist_rad": 0.6}

        def send(points, durations):
            self.assertTrue(s._holding)
            self.assertEqual(s.manipulation._grip_target, -0.6)
            self.assertEqual(durations, [0.4, 0.6])
            self.assertEqual(points[0][:5], [0, 0.2, 0.3, 0.4, 1.2])
            self.assertEqual(points[1][:4], [0, 0.2, 0.3, 0.4])
            self.assertAlmostEqual(points[1][4], 0.6)
            self.assertGreater(points[0][5], points[1][5])
            return True

        s.manipulation._send_trajectory.side_effect = send
        self.assertTrue(s._close_grip())
        s.manipulation.gripper_close.assert_not_called()

    def test_close_twist_failure_keeps_grip_for_teardown(self):
        s = self.s
        s._arm_joints = lambda: [0] * 5 + [1.0]
        s._p = {**s._p, "close_strength": 0.6, "twist_rad": 0.6}
        s.manipulation._send_trajectory.return_value = False
        with self.assertRaises(RuntimeError):
            s._close_grip()
        self.assertTrue(s._holding)
        self.assertEqual(s.manipulation._grip_target, -0.6)

    def test_bad_free_space_endpoint_never_descends_or_closes(self):
        self.s.manipulation.pose = NS(x=0.4, y=0, z=0.08)
        with self.assertRaises(RuntimeError):
            self.s._grasp_at("sock", (0.315, 0))
        self.assertEqual(self.s.manipulation.follow.call_count, 1)
        self.s._close_twist_lift.assert_not_called()

    def test_inherited_sequence_twists_once_then_lifts(self):
        tree = ast.parse((ROOT / "innate_skills/pick_any_object.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PickAnyObject")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_close_twist_lift")
        env = dict(ArmFailed=RuntimeError, ArmUnhealthy=RuntimeError)
        exec(compile(ast.Module(body=[method], type_ignores=[]), "sequence", "exec"), env)
        s = self.s
        s._arm_joints = lambda: [0, 0.2, 0.3, 0.4, 0, 1.0]
        s._p = {**s._p, "close_strength": 0.6, "twist_rad": 0.6, "lift_rad": 0.5, "close_settle_max_s": 0.2}
        s._lift_before_close = Mock()
        s._fingers_still = Mock()
        events = []
        s.manipulation._send_trajectory.side_effect = lambda *a: events.append("close-twist") or True
        s.manipulation.move_joints.side_effect = lambda *a, **k: events.append("lift")
        env["_close_twist_lift"](s, 0.265, 0, 0, 1.3, 0)
        self.assertEqual(events, ["close-twist", "lift"])
        self.assertEqual(s.manipulation.move_joints.call_args.args[0][5], -0.6)

    def test_local_navigation_cannot_request_map_frame(self):
        call = Mock(return_value="ok")
        base = type("Base", (), {"execute": call})
        cls = load(ROOT / "innate_skills/navigate_locally.py", dict(NavigateToPosition=base, SkillReturn=str))[
            "NavigateLocally"
        ]
        cls().execute(0.5, 0.1, 45)
        call.assert_called_once_with(0.5, 0.1, theta_degrees=45, local_frame=True)

    def test_agent_no_gaze_or_memory_and_exact_skills(self):
        names = ["TurnInPlace", "NavigateLocally", "PickSockFast", "DropInBoxFast", "Wave", "MicroInput"]
        env = {n: type(n, (), {}) for n in names}
        env.update(Agent=object, SkillRef=object, InputRef=object)
        cls = load(ROOT / "innate_agents/sock_demo_agent.py", env)["SockDemoAgent"]
        self.assertFalse(cls().uses_gaze())
        self.assertEqual([c.__name__ for c in cls().get_skills()], names[:-1])


if __name__ == "__main__":
    unittest.main()
