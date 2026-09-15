import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))
from engine import ArmWorld  # noqa: E402
from pose_study import catalogue  # noqa: E402


class PersonalEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        neutral = catalogue(ArmWorld())[0]
        cls.workspace = {"center": neutral["ee"], "span": [0.045, 0.1, 0.13], "angles": neutral["angles"], "grip": 0.55}

    def test_repeated_interruptions_cannot_accumulate_pitch(self):
        world = ArmWorld(self.workspace)
        for pitch in [-0.5, 0, 0.5, 0, -0.5, 0.5, 0]:
            for i in range(180):
                world.move(0, 0, 0, 0.55, wrist=[0, pitch, 0])
                world.tick(1 / 60)
                if i == 45:
                    world.hold("tracking_lost")
            snapshot = world.snapshot()
            self.assertAlmostEqual(snapshot["wrist_measured"][1], pitch, delta=0.025)
            self.assertLess(np.linalg.norm(np.array(snapshot["ee"]) - self.workspace["center"]), 0.005)

    def test_pitch_returns_to_requested_angle_after_reach_limit(self):
        world = ArmWorld(self.workspace)
        for _ in range(150):
            world.move(1, -1, 1, 0.55, wrist=[0, -0.65, 0])
            world.tick(1 / 60)
        world.hold("paused")
        for _ in range(300):
            world.move(0, 0, 0, 0.55, wrist=[0.7, 0.4, 0])
            world.tick(1 / 60)
        state = world.snapshot()
        self.assertAlmostEqual(state["wrist_measured"][0], 0.7, delta=0.025)
        self.assertAlmostEqual(state["wrist_measured"][1], 0.4, delta=0.025)
        world.hold("paused")
        self.assertAlmostEqual(world.snapshot()["wrist"][1], state["wrist_measured"][1], delta=0.001)

    def test_close_reach_cannot_force_the_wrong_taught_pitch(self):
        world = ArmWorld(self.workspace)
        target = np.array([0.309, -0.086, 0.157])
        offset = ((target - world.center) / world.span)[[1, 2, 0]]
        for _ in range(240):
            previous = world.path_target.copy()
            world.move(*offset, 0.55, wrist=[0, 0.434, 0])
            world.tick(1 / 60)
            self.assertLessEqual(np.linalg.norm(world.path_target - previous), 0.18 / 60 + 1e-9)
        state = world.snapshot()
        self.assertAlmostEqual(state["wrist_measured"][1], 0.434, delta=0.025)
        self.assertLess(np.linalg.norm(np.array(state["ee"]) - target), 0.04)
        self.assertTrue(state["rotation_limited"])

    def test_floor_approach_closure_lift_and_vertical_pitch(self):
        world = ArmWorld(self.workspace)
        for height, pitch, grip in [
            (0.07, math.radians(80), 0.55),
            (0.012, math.radians(80), 1),
            (0.012, math.radians(80), 0),
            (0.09, math.radians(80), 0),
            (0.02, math.pi / 2, 0),
        ]:
            target = np.array([0.32, world.center[1], height])
            offset = ((target - world.center) / world.span)[[1, 2, 0]]
            for _ in range(300):
                before = np.array([world.sim.joint_targets()[n] for n in world.names])
                before_path = world.path_target.copy()
                world.move(*offset, grip, wrist=[0, pitch, 0])
                world.tick(1 / 60)
                after = np.array([world.sim.joint_targets()[n] for n in world.names])
                self.assertLessEqual(float(np.max(np.abs(after - before))), 1 / 60 + 1e-9)
                self.assertLessEqual(np.linalg.norm(world.path_target - before_path), 0.18 / 60 + 1e-9)
            state = world.snapshot()
            np.testing.assert_allclose(state["ee"], target, atol=0.004)
            self.assertAlmostEqual(state["wrist_measured"][1], pitch, delta=0.025)
            self.assertAlmostEqual(state["grip"], grip, delta=0.02)
            self.assertGreater(min((c.dist for c in world.ground_contacts()), default=0), -0.0005)
        # Holding a deep tilt must preserve it when the live mapper reanchors.
        world.hold()
        held = world.snapshot()
        self.assertAlmostEqual(held["wrist"][1], math.pi / 2, delta=0.01)
        for _ in range(180):
            world.move(*held["offset"], held["grip"], wrist=held["wrist"])
            world.tick(1 / 60)
        self.assertAlmostEqual(world.snapshot()["wrist_measured"][1], math.pi / 2, delta=0.025)

    def test_personal_pitch_range_is_asymmetric_and_still_bounded(self):
        world = ArmWorld(self.workspace)
        for pitch in [math.pi / 2 + 0.01, -0.66]:
            with self.assertRaises(ValueError):
                world.move(0, 0, wrist=[0, pitch, 0])

    def test_yaw_reach_projection_and_floor_grasp_work_on_both_sides(self):
        world = ArmWorld(self.workspace)
        for yaw in [-0.4, 0.4]:
            for target, pitch in [
                (np.array([0.309, -0.086, 0.157]), 0.434),
                (np.array([0.32, world.center[1], 0.012]), math.radians(80)),
            ]:
                offset = ((target - world.center) / world.span)[[1, 2, 0]]
                for _ in range(300):
                    world.move(*offset, 0, wrist=[0, pitch, yaw])
                    world.tick(1 / 60)
                state = world.snapshot()
                expected = world.swivel(target, yaw)
                self.assertLess(np.linalg.norm(np.array(state["ee"]) - expected), 0.04)
                self.assertAlmostEqual(state["wrist_measured"][1], pitch, delta=0.025)
                actual_heading = math.atan2(state["ee"][1] - world.shoulder[1], state["ee"][0] - world.shoulder[0])
                expected_heading = math.atan2(target[1] - world.shoulder[1], target[0] - world.shoulder[0]) + yaw
                self.assertAlmostEqual(actual_heading, expected_heading, delta=0.015)
                self.assertGreater(min((c.dist for c in world.ground_contacts()), default=0), -0.0005)

    def test_reanchor_during_a_yaw_turn_preserves_the_achieved_pose(self):
        world = ArmWorld(self.workspace)
        for _ in range(12):
            world.move(0, 0, 0, 0.55, wrist=[0, 0, 0.4])
            world.tick(1 / 60)
        world.hold("tracking_lost")
        held = world.snapshot()
        for _ in range(180):
            world.move(*held["offset"], held["grip"], wrist=held["wrist"])
            world.tick(1 / 60)
        np.testing.assert_allclose(world.snapshot()["ee"], held["ee"], atol=0.004)


if __name__ == "__main__":
    unittest.main()
