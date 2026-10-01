"""No-motion rehearsal contract through skill execute and real follower commands."""

import math
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

import test_box_marker
from test_box_marker import load_marker
from test_sock_demo import ROOT, load


class StationarySockTests(unittest.TestCase):
    def pickup(self):
        base = type(
            "Base", (), {"_p": dict(tilt_deg=-25, nav_arm_s=0.8, grasp_x_off=0.05, hover_z=0.15, arm_pitch=1.3)}
        )
        grasp = Mock()
        cls = load(
            ROOT / "innate_skills/pick_sock_stationary.py",
            dict(
                math=math,
                PickSockFast=base,
                PickAnyObject=NS(_grasp_at=grasp),
                Llm=lambda *a, **k: NS(available=True),
                SkillReturn=str,
                SkillFailed=RuntimeError,
                NAV_ARM=[0] * 5,
                IMG_W=640,
                IMG_H=480,
                pixel_to_floor=lambda *a: (0.3, 0),
            ),
        )["PickSockStationary"]
        s = cls()
        s.mobility = Mock(spec=["stop"])  # any drive/navigation access fails
        s.manipulation = Mock()
        s.manipulation.clamp_reach.side_effect = lambda x, y: (x, y)
        s.manipulation.reachable.return_value = True
        s.head = Mock()
        s.overlay = Mock()
        s.check_cancelled = Mock()
        s._detect_px = Mock(return_value=(320, 380))
        s._grasp_verified = Mock(return_value=True)
        s._rest_arm = Mock()
        return s, grasp

    def test_pickup_uses_only_arm_and_stop(self):
        s, grasp = self.pickup()
        self.assertIn("without moving", s.execute("blue sock"))
        grasp.assert_called_once_with(s, "blue sock", (0.3, 0))
        self.assertEqual(s.mobility.stop.call_count, 2)

    def test_missing_unreachable_and_cancelled_never_drive(self):
        for mode in ("missing", "clamped", "unreachable", "cancelled", "empty"):
            with self.subTest(mode=mode):
                s, grasp = self.pickup()
                if mode == "missing":
                    s._detect_px.return_value = None
                if mode == "clamped":
                    s.manipulation.clamp_reach.side_effect = lambda x, y: (0.2, y)
                if mode == "unreachable":
                    s.manipulation.reachable.return_value = False
                if mode == "cancelled":
                    s.check_cancelled.side_effect = RuntimeError("cancelled")
                if mode == "empty":
                    s._grasp_verified.return_value = False
                with self.assertRaises(RuntimeError):
                    s.execute("blue sock")
                s.mobility.stop.assert_called()
                s._rest_arm.assert_called_once()
                if mode != "empty":
                    grasp.assert_not_called()

    def test_drop_has_no_retreat_and_zero_translation_follower(self):
        e = load_marker()
        dock = Mock()
        config = dict(head_tilt_deg=-12, near_xy=(0.23, 0), image_size=[640, 480])
        cls = load(
            ROOT / "innate_skills/drop_in_box_stationary.py",
            dict(
                DropInBoxFast=object,
                SkillReturn=str,
                SkillFailed=RuntimeError,
                load_config=lambda: config,
                MarkerFollower=e["MarkerFollower"],
                MarkerDock=object,
                math=math,
                IMG_W=640,
                fresh_sock_held=lambda s: True,
            ),
        )["DropInBoxStationary"]
        cls.execute.__globals__["StationaryBoxDock"] = dock
        s = cls()
        s._p = dict(tilt_deg=-12, drop_inset=0.08, drop_inset_min=0.04, carry_x=0.2, arm_pitch=1.3, travel_joints=[])
        s.RELEASE_Z = 0.24
        s.CLEARANCE_Z = 0.26
        s.manipulation = Mock()
        s.manipulation.clamp_reach.return_value = (0.31, 0)
        s.manipulation.reachable.return_value = True
        s.mobility = Mock()
        s.head = Mock()
        s.overlay = Mock()
        s.wait_for = Mock()
        s._holding = Mock(return_value=True)
        s._secure_grip = Mock()
        s._carry_pose = Mock()
        s._release_at = Mock()
        s._retract = Mock()
        s.execute()
        follower = dock.return_value.run.call_args.kwargs["follower"]
        self.assertEqual((follower.max_linear, follower.max_reverse), (0, 0))
        for linear in (1.0, -1.0, 0.15, -0.08):
            follower._send_cmd(linear, 0.3)
        for call in s.mobility.send_cmd_vel.call_args_list:
            self.assertEqual(call.kwargs["linear_x"], 0)
        s._release_at.assert_called_once_with(0.23, 0)
        s.mobility.stop.assert_called_once()

    def test_stationary_accepts_centered_tag_independent_of_size(self):
        for scale in (0.7, 1.12, 1.4):
            e = load_marker()
            helper = test_box_marker.MarkerGeometryTests()
            helper.e = e
            clock = [0.0]
            e["time"] = NS(monotonic=lambda clock=clock: clock[0])
            h = NS(main_image=NS(gray="frame"), mobility=Mock(), logger=Mock(), check_cancelled=Mock())

            def sleep(dt, clock=clock, h=h):
                clock[0] += dt
                h.main_image = NS(gray="frame")

            h.sleep = sleep
            env = load(
                ROOT / "innate_skills/drop_in_box_stationary.py",
                dict(
                    math=math,
                    IMG_W=640,
                    MarkerDock=e["MarkerDock"],
                    MarkerFollower=e["MarkerFollower"],
                    DropInBoxFast=object,
                    SkillReturn=str,
                ),
            )
            dock = env["StationaryBoxDock"](h, helper.config())
            center = dock.target_quad.mean(axis=0)
            quad = (dock.target_quad - center) * scale + center + [20, 25]
            dock.detector = NS(detect_quad=lambda gray, quad=quad: quad)
            follower = env["StationaryBoxFollower"](h.mobility)
            self.assertEqual(dock.run(follower), (0.23, 0))
            self.assertLess(clock[0], 3)
            for c in h.mobility.send_cmd_vel.call_args_list:
                self.assertEqual(c.kwargs["linear_x"], 0)
            follower._send_cmd = Mock()
            follower._drive_toward(dock.target_quad + [100, 0], target_center_x=center[0])
            self.assertLess(follower._send_cmd.call_args.args[1], -0.6)
            follower._drive_toward(dock.target_quad + [-100, 0], target_center_x=center[0])
            self.assertGreater(follower._send_cmd.call_args.args[1], 0.6)

    def test_agent_exposes_only_rotation_arm_skills_and_no_microphone(self):
        names = ["TurnInPlace", "PickSockStationary", "DropInBoxStationary", "Wave"]
        env = {n: type(n, (), {}) for n in names}
        env["SockRehearsedAgent"] = type("Base", (), {"get_inputs": lambda s: []})
        cls = load(ROOT / "innate_agents/gemini_sock_stationary_agent.py", env)["GeminiSockStationaryAgent"]
        self.assertEqual([s.__name__ for s in cls().get_skills()], names)
        self.assertEqual(cls().get_inputs(), [])
        self.assertEqual(cls.model, "google:gemini-3.6-flash")
        self.assertEqual(cls.model_extra_body, "{}")
