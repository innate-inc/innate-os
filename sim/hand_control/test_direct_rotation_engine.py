"""Unit-gain rotation checked against measured MuJoCo joints and contacts."""

import json
import math
import sys
import unittest
from concurrent.futures import Future
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))
from engine import MAX_JOINT_SPEED, ArmWorld
from pose_study import catalogue


class DirectRotationEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pose = catalogue(ArmWorld())[0]
        cls.workspace = dict(
            center=pose["ee"],
            span=[0.045, 0.1, 0.13],
            angles=pose["angles"],
            grip=0.55,
            pinch_point=True,
            direct_rotation=True,
        )

    def drive_pose(self, world, degrees, x=0.35, z=0.13, grip=1):
        offset = ((np.array([x, world.center[1], z]) - world.center) / world.span)[[1, 2, 0]]
        previous = np.array([world.sim.joint_targets()[n] for n in world.names])
        for _ in range(360):
            world.move(*offset, grip, wrist=np.radians(degrees).tolist())
            world.tick(1 / 60)
            current = np.array([world.sim.joint_targets()[n] for n in world.names])
            self.assertLessEqual(max(abs(current - previous)), MAX_JOINT_SPEED / 60 + 1e-9)
            self.assertGreaterEqual(min((c.dist for c in world.ground_contacts()), default=0), -0.0005)
            previous = current
        state = world.snapshot()
        json.dumps(state, allow_nan=False)
        return state

    def test_each_axis_and_combined_rotations_reach_the_requested_angle(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)
        for requested in ([40, 0, 0], [-40, 0, 0], [0, 30, 0], [0, -30, 0], [20, 25, 0]):
            with self.subTest(requested=requested):
                state = self.drive_pose(world, requested)
                np.testing.assert_allclose(np.degrees(state["wrist_measured"]), requested, atol=0.5)
                self.assertFalse(state["rotation_limited"])
                self.assertLess(state["error_mm"], 2)
                self.assertEqual(state["ik_solver"], "innate_kdl")

    def test_impossible_yaw_keeps_the_grasp_point_instead_of_sweeping_sideways(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)
        expected = np.array([0.35, world.center[1], 0.13])
        for yaw in (60, -60, 30, -30, 0):
            state = self.drive_pose(world, [0, 0, yaw])
            np.testing.assert_allclose(state["target"], expected, atol=1e-10)
            self.assertLess(np.linalg.norm(np.array(state["ee"]) - expected), 0.002)
            self.assertLess(abs(state["wrist_measured"][2]), math.radians(1))
            self.assertEqual(state["rotation_limited"], yaw != 0)

    def test_roll_has_no_sideways_sweep_during_motion(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)
        self.drive_pose(world, [0, 0, 0])
        center = world.grasp_point().copy()
        offset = ((np.array([0.35, world.center[1], 0.13]) - world.center) / world.span)[[1, 2, 0]]
        for roll in (40, -40, 0):
            for _ in range(180):
                world.move(*offset, 1, wrist=[math.radians(roll), 0, 0])
                world.tick(1 / 60)
                self.assertLess(abs(world.grasp_point()[1] - center[1]), 0.002)

    def test_quarter_turn_roll_aligns_the_actual_jaws_vertically_at_the_same_grasp_point(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)
        for roll in (90, 0, -90):
            state = self.drive_pose(world, [roll, 0, 0])
            gap = np.diff(world.data.geom_xpos[world.pads], axis=0)[0]
            gap /= np.linalg.norm(gap)
            self.assertGreater(abs(gap[2 if roll else 1]), 0.999)
            self.assertLess(state["error_mm"], 2)
            self.assertAlmostEqual(np.degrees(state["wrist_measured"][0]), roll, delta=0.5)

    def test_ik_failure_holds_all_joints_and_closes_the_pending_grip_command(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)

        class BrokenIK:
            def solve(self, *args):
                raise RuntimeError("Test worker unavailable")

        world.innate_ik = BrokenIK()
        world.move(0, 0, 0, 0, wrist=[0.5, 0, 0])
        world.tick(1 / 60)
        self.assertFalse(world.active)
        self.assertEqual(world.reason, "ik_unavailable")
        self.assertIn("unavailable", world.ik_error)
        self.assertAlmostEqual(world.sim.joint_targets()["joint6"], world.data.qpos[world.grip_qadr], delta=0.025)

    def test_a_pending_ik_result_cannot_move_the_arm_after_pause_and_restart(self):
        world = ArmWorld(self.workspace)
        jobs = []

        class DelayedIK:
            def submit(self, *args):
                future = Future()
                jobs.append(future)
                return future

        world.innate_ik = DelayedIK()
        world.move(0, 0, 0, 0.55, wrist=[0.5, 0, 0])
        world.tick(1 / 60)
        self.assertEqual(len(jobs), 1)
        world.hold()
        held = world.command.copy()
        stale = held.copy()
        stale[4] = 0.5
        jobs[0].set_result({"solution": stale.tolist(), "grasp_error": 0, "rotation_error": 0})
        world.move(*world.snapshot()["offset"], 0.55, wrist=world.wrist.tolist())
        world.tick(1 / 60)
        np.testing.assert_allclose(world.command, held)
        self.assertEqual(len(jobs), 2)
        jobs[1].set_result({"solution": held.tolist(), "grasp_error": 0, "rotation_error": 0})
        world.tick(1 / 60)
        json.dumps(world.snapshot(), allow_nan=False)

    def test_old_browser_cannot_restart_the_incorrect_axis_mapping(self):
        from server import ControlSession

        world = ArmWorld(self.workspace)
        control = ControlSession(world)
        with self.assertRaisesRegex(ValueError, "Refresh"):
            control.handle("old-tab", {"op": "begin"})
        self.assertIsNone(control.owner)
        self.assertEqual(
            control.handle("new-tab", {"op": "begin", "controller": "innate-ik-camera-v1"})["type"], "begun"
        )

    def test_downward_grasp_preserves_safe_roll_near_the_floor(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)
        for roll, grip in ((20, 1), (60, 1), (-60, 0), (20, 1)):
            state = self.drive_pose(world, [roll, 80, 0], x=0.32, z=0.012, grip=grip)
            # The OS solver's weighted 1e-4 tolerance plus simulated servo sag
            # allows sub-degree tilt error while preserving the grasp point.
            np.testing.assert_allclose(np.degrees(state["wrist_measured"]), [roll, 80, 0], atol=1)
            self.assertFalse(state["rotation_limited"])
            self.assertLess(state["error_mm"], 3)
            self.assertAlmostEqual(state["grip"], grip, delta=0.02)

    def test_roll_stops_at_floor_boundary_instead_of_reducing_every_angle(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)
        self.drive_pose(world, [0, 0, 0], z=0.03)
        world.anticipate_grasp()
        self.assertAlmostEqual(world.safe_roll(math.radians(5), 0, 0.03), math.radians(5))
        limited = world.safe_roll(math.radians(80), 0, 0.03)
        self.assertGreater(limited, math.radians(5))
        self.assertLess(limited, math.radians(80))
        state = self.drive_pose(world, [80, 0, 0], z=0.03)
        self.assertTrue(state["rotation_limited"])
        self.assertAlmostEqual(state["wrist_measured"][0], limited, delta=0.01)

    def test_pause_preserves_achieved_roll_beyond_the_old_range(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)
        state = self.drive_pose(world, [75, 0, 0])
        world.hold()
        self.assertFalse(world.active)
        self.assertAlmostEqual(world.wrist[0], state["wrist_measured"][0])
        self.assertGreater(world.wrist[0], 1.2)

    def test_interrupted_yaw_reanchors_to_the_achieved_heading_without_a_jump(self):
        world = ArmWorld(self.workspace, synchronous_ik=True)
        for _ in range(15):
            world.move(0.25, 0, 0, 0.55, wrist=[0, 0, math.radians(60)])
            world.tick(1 / 60)
        measured = world.snapshot()["wrist_measured"][2]
        self.assertLess(measured, math.radians(40))
        world.hold()
        held = world.snapshot()
        self.assertAlmostEqual(held["wrist"][2], measured)
        self.assertLess(max(abs(np.array(held["offset"]))), 1)
        world.move(*held["offset"], held["grip"], wrist=held["wrist"])
        np.testing.assert_allclose(world.desired, held["ee"], atol=1e-9)


if __name__ == "__main__":
    unittest.main()
