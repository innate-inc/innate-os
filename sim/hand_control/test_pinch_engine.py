"""Verify measured finger-pad control through the real simulator physics."""

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))
from engine import WATCHDOG_S, ArmWorld
from pose_study import catalogue


class PinchEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pose = catalogue(ArmWorld())[0]
        cls.workspace = dict(
            center=pose["ee"], span=[0.045, 0.1, 0.13], angles=pose["angles"], grip=0.55, pinch_point=True
        )

    def test_pad_midpoint_stays_at_grasp_target_through_closing_and_reopening(self):
        world = ArmWorld(self.workspace)
        for x, z, pitch in [(0.35, 0.13, 0), (0.32, 0.07, math.radians(80)), (0.32, 0.012, math.radians(80))]:
            target = np.array([x, world.center[1], z])
            offset = ((target - world.center) / world.span)[[1, 2, 0]]
            for _ in range(360):
                world.move(*offset, 1, wrist=[0, pitch, 0])
                world.tick(1 / 60)
            for grip in [1, 0, 1]:
                errors = []
                for _ in range(240):
                    world.move(*offset, grip, wrist=[0, pitch, 0])
                    world.tick(1 / 60)
                    errors.append(np.linalg.norm(world.grasp_point() - target))
                actual = world.data.geom_xpos[world.pads].mean(axis=0)
                np.testing.assert_allclose(world.snapshot()["ee"], actual, atol=1e-12)
                self.assertLess(np.linalg.norm(actual - target), 0.004)
                self.assertLess(max(errors), 0.007 if z < 0.03 else 0.003)
                self.assertAlmostEqual(world.snapshot()["wrist_measured"][1], pitch, delta=0.025)
                self.assertAlmostEqual(world.snapshot()["grip"], grip, delta=0.02)

    def test_pause_and_watchdog_cancel_inflight_gripper_target(self):
        for timeout in [False, True]:
            world = ArmWorld(self.workspace)
            world.move(0, 0, 0, 0, now=10, wrist=[0, 0, 0])
            for i in range(6):
                world.tick(1 / 60, now=10 + i / 60)
            measured = float(world.data.qpos[world.grip_qadr])
            if timeout:
                world.tick(1 / 60, now=10 + WATCHDOG_S + 0.01)
            else:
                world.hold("paused")
            self.assertFalse(world.active)
            self.assertAlmostEqual(world.sim.joint_targets()["joint6"], measured, delta=1e-10)
            for _ in range(60):
                world.tick(1 / 60, now=11)
            self.assertAlmostEqual(world.data.qpos[world.grip_qadr], measured, delta=0.025)


if __name__ == "__main__":
    unittest.main()
