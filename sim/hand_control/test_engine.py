import asyncio
import itertools
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

import numpy as np  # noqa: E402
from engine import GROUND_Z, ArmWorld  # noqa: E402
from server import ControlSession, StatePublisher  # noqa: E402


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = ArmWorld()

    def setUp(self):
        self.world.hold()

    def test_boot_starts_at_ground(self):
        world = ArmWorld()
        self.assertAlmostEqual(world.snapshot()["ee"][2], GROUND_Z, delta=0.004)
        self.assertFalse(world.active)
        self.assertLess(world.snapshot()["height_mm"], 4)

    def test_floor_stays_at_ground_across_reach_and_grip(self):
        for h, r, grip in itertools.product([-1, 1], [-1, 1], [0, 1]):
            for _ in range(180):
                self.world.move(h, -1, r, grip)
                self.world.tick(1 / 60)
            self.assertEqual(self.world.desired[2], GROUND_Z)
            # Closed jaws can touch first, stopping the virtual grasp point
            # above zero. Check real floor contact instead of forcing through it.
            contacts = self.world.ground_contacts()
            if contacts:
                self.assertLess(self.world.snapshot()["ee"][2], 0.015)
                self.assertGreater(min(c.dist for c in contacts), -0.0005)
                self.assertEqual(self.world.snapshot()["height_mm"], 0)
            else:
                self.assertAlmostEqual(self.world.snapshot()["ee"][2], GROUND_Z, delta=0.005)
            self.assertGreater(self.world.snapshot()["ee"][2], -0.002)

    def test_workspace_and_physical_motion(self):
        for h, v, r in [(0, 0, 0), *itertools.product([-1, 1], repeat=3), (0, 0, 0)]:
            for _ in range(180):
                previous = np.array([self.world.sim.joint_targets()[name] for name in self.world.names])
                self.world.move(h, v, r)
                self.world.tick(1 / 60)
                current = np.array([self.world.sim.joint_targets()[name] for name in self.world.names])
                self.assertLessEqual(float(np.max(np.abs(current - previous))), 1 / 60 + 1e-9)
            self.assertLess(self.world.snapshot()["error_mm"], 5, (h, v, self.world.snapshot()))

    def test_missing_input_holds_and_does_not_resume(self):
        self.world.move(1, 1)
        self.world.tick(1 / 60)
        self.world.tick(1 / 60, now=time.monotonic() + 1)
        self.assertFalse(self.world.active)
        self.assertEqual(self.world.reason, "input_timeout")
        held = self.world.desired.copy()
        for _ in range(40):
            self.world.tick(1 / 60)
        np.testing.assert_array_equal(self.world.desired, held)

    def test_invalid_values_are_rejected(self):
        for h, v in [(float("nan"), 0), (0, float("inf")), (True, 0), (1.1, 0), ("1", 0)]:
            with self.assertRaises(ValueError):
                self.world.move(h, v)

    def settle_wrist(self, wrist, vertical=0, grip=1):
        for _ in range(240):
            self.world.move(0, vertical, 0, grip, wrist=wrist)
            self.world.tick(1 / 60)
        state = self.world.snapshot()
        json.dumps(state, allow_nan=False)
        return state

    def test_roll_and_pitch_preserve_grasp_position(self):
        self.world = ArmWorld()
        self.settle_wrist([0, 0, 0])
        # Move off the downward joint limit before testing both directions.
        initial = self.settle_wrist([0, -0.2, 0])
        for roll, pitch in [(0.5, -0.2), (-0.5, -0.2), (0, -0.08), (0, -0.32), (0.4, -0.12)]:
            state = self.settle_wrist([roll, pitch, 0])
            self.assertLess(state["error_mm"], 5)
            self.assertAlmostEqual(state["wrist_measured"][0], roll, delta=0.03)
            self.assertAlmostEqual(state["wrist_measured"][1] - initial["wrist_measured"][1], pitch + 0.2, delta=0.035)
        before = np.array(state["wrist_measured"])
        closed = self.settle_wrist([0.4, -0.12, 0], grip=0)
        np.testing.assert_allclose(closed["wrist_measured"], before, atol=0.03)
        self.assertLess(closed["grip"], 0.02)
        self.assertLess(closed["error_mm"], 5)

    def test_translation_preserves_pitch_and_reversing_a_limit_responds_immediately(self):
        self.world = ArmWorld()
        self.settle_wrist([0, 0, 0])
        initial = self.settle_wrist([0, -0.25, 0])
        for vertical in [-0.15, -0.3, 0]:
            state = self.settle_wrist([0, -0.25, 0], vertical=vertical)
            self.assertLess(state["error_mm"], 5)
            self.assertAlmostEqual(state["wrist_measured"][1], initial["wrist_measured"][1], delta=0.015)
        limited = self.settle_wrist([0, 0.65, 0])
        self.assertTrue(limited["rotation_limited"])
        reversed = self.settle_wrist([0, 0.60, 0])
        self.assertAlmostEqual(limited["wrist_measured"][1] - reversed["wrist_measured"][1], 0.05, delta=0.015)

    def test_pitch_responds_at_ground_without_a_height_dead_zone(self):
        world = ArmWorld()
        initial = world.snapshot()["wrist_measured"][1]
        for pitch in [-0.2, 0.2, 0]:
            for _ in range(180):
                world.move(0, -1, wrist=[0, pitch, 0])
                world.tick(1 / 60)
            self.assertAlmostEqual(world.snapshot()["wrist_measured"][1] - initial, pitch, delta=0.03)
            self.assertGreater(world.snapshot()["ee"][2], -0.002)
            self.assertGreater(min((c.dist for c in world.ground_contacts()), default=0), -0.0005)

    def test_yaw_swivels_the_real_base_joint_and_preserves_hand_anchor(self):
        initial = self.settle_wrist([0, 0, 0])
        for yaw in [-0.3, 0.3]:
            state = self.settle_wrist([0, 0, yaw])
            self.assertAlmostEqual(state["wrist_measured"][2] - initial["wrist_measured"][2], yaw, delta=0.015)
            expected = self.world.swivel(initial["target"], yaw)
            np.testing.assert_allclose(state["ee"], expected, atol=0.005)
            np.testing.assert_allclose(state["offset"], [0, 0, 0], atol=1e-8)

    def test_unreachable_pitch_clamps_without_sacrificing_position(self):
        state = self.settle_wrist([0.6, 0.65, 0.35])
        self.assertTrue(state["rotation_limited"])
        self.assertLess(state["error_mm"], 5)
        state = self.settle_wrist([0.6, 0.65, 0.35], vertical=-1)
        self.assertTrue(state["rotation_limited"])
        self.assertAlmostEqual(state["wrist_measured"][0], 0, delta=0.03)
        self.assertGreater(state["ee"][2], -0.002)
        self.assertLess(state["height_mm"], 5)

    def test_invalid_rotations_are_rejected_without_changing_target(self):
        previous = self.world.desired.copy()
        for wrist in [[1.3, 0, 0], [0, 0.7, 0], [0, 0, 0.5], [True, 0, 0], [float("nan"), 0, 0], [0, 0], "roll"]:
            with self.assertRaises(ValueError):
                self.world.move(0, 0, wrist=wrist)
            np.testing.assert_array_equal(self.world.desired, previous)

    def test_reanchoring_uses_achieved_rotation_after_an_interrupted_turn(self):
        self.settle_wrist([0.2, 0.08, 0.1])
        self.world.move(0, 0, wrist=[1.2, 0.65, 0.45])
        self.world.tick(1 / 60)
        self.world.hold()
        held = self.world.snapshot()
        self.assertLess(abs(held["wrist"][0]), 0.3)
        h, v, r = held["offset"]
        for _ in range(180):
            self.world.move(h, v, r, wrist=held["wrist"])
            self.world.tick(1 / 60)
        resumed = self.world.snapshot()
        np.testing.assert_allclose(resumed["ee"], held["ee"], atol=0.004)
        np.testing.assert_allclose(resumed["wrist_measured"], held["wrist_measured"], atol=0.03)

    def test_start_without_frames_releases_the_session(self):
        control = ControlSession(self.world)
        control.handle(object(), {"op": "begin"})
        control.expire(time.monotonic() + 3)
        self.assertIsNone(control.owner)
        self.assertFalse(self.world.active)

    def test_session_ownership_stop_tokens_order_and_stale_frames(self):
        control = ControlSession(self.world)
        owner, other = object(), object()
        began = control.handle(owner, {"op": "begin", "request": 1})
        with self.assertRaisesRegex(ValueError, "Another tab"):
            control.handle(other, {"op": "begin"})
        msg = {
            "op": "move",
            "token": began["token"],
            "sequence": 1,
            "captured_at": 1000,
            "horizontal": 0,
            "vertical": 0,
            "reach": 0,
            "grip": 1,
        }
        control.handle(owner, msg, wall_ms=1020)
        with self.assertRaisesRegex(ValueError, "sequence"):
            control.handle(owner, msg, wall_ms=1020)
        control.handle(other, {"op": "stop"})
        self.assertTrue(self.world.active)
        control.handle(owner, {"op": "stop"})
        self.assertFalse(self.world.active)
        with self.assertRaises(ValueError):
            control.handle(owner, {**msg, "sequence": 2}, wall_ms=1050)
        second = control.handle(owner, {"op": "begin"})
        self.assertNotEqual(second["token"], began["token"])
        held = control.handle(owner, {**msg, "token": second["token"]}, wall_ms=1800)
        self.assertEqual(held["type"], "held")
        self.assertIs(control.owner, owner)
        self.assertFalse(self.world.active)
        control.handle(owner, {**msg, "token": second["token"], "sequence": 2, "captured_at": 1800}, wall_ms=1810)
        self.assertTrue(self.world.active)

    def test_gripper_opens_closes_and_holds_in_real_physics(self):
        for grip in [1, 0, 0.5, 1]:
            for _ in range(120):
                self.world.move(0, 0, 0, grip)
                self.world.tick(1 / 60)
            self.assertAlmostEqual(self.world.snapshot()["grip"], grip, delta=0.015)
            self.assertLess(self.world.snapshot()["error_mm"], 5)
        self.world.move(0, 0, 0, 0)
        self.world.tick(1 / 60)
        self.world.hold()
        target = self.world.sim.joint_targets()["joint6"]
        for _ in range(60):
            self.world.tick(1 / 60)
        self.assertEqual(self.world.sim.joint_targets()["joint6"], target)

    def test_heartbeat_preserves_session_but_never_keeps_motion_alive(self):
        control = ControlSession(self.world)
        owner = object()
        token = control.handle(owner, {"op": "begin"})["token"]
        self.world.move(0.5, 0.5)
        control.handle(owner, {"op": "keepalive", "token": token})
        self.world.tick(1 / 60, now=time.monotonic() + 0.5)
        self.assertFalse(self.world.active)
        self.assertIs(control.owner, owner)
        self.world.move(0, 0)
        control.handle(owner, {"op": "hold", "token": token})
        self.assertFalse(self.world.active)
        self.assertIs(control.owner, owner)


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_viewer_gets_latest_state_without_blocking_or_disconnecting(self):
        class SlowSocket:
            def __init__(self):
                self.sent = []
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.closed = False

            async def send(self, payload):
                self.started.set()
                await self.release.wait()
                self.sent.append(payload)

            async def close(self, **kwargs):
                self.closed = True

        socket = SlowSocket()
        publisher = StatePublisher(socket)
        task = asyncio.create_task(publisher.run())
        try:
            publisher.publish("first")
            await socket.started.wait()
            # An 80 ms send used to silently discard this viewer after 25 ms.
            await asyncio.sleep(0.08)
            for index in range(100):
                publisher.publish(str(index))
            self.assertEqual(publisher.pending.qsize(), 1)
            self.assertFalse(socket.closed)
            socket.release.set()
            await asyncio.sleep(0.02)
            self.assertEqual(socket.sent, ["first", "99"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
