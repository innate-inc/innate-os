import ast
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

P = Path(__file__).parents[1] / "innate_skills/approach.py"


class EasingTests(unittest.TestCase):
    def setUp(self):
        self.t = 0.0
        tree = ast.parse(P.read_text())
        ramp = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ApproachRamp")
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FloorApproach")
        move = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_eased_move")
        env = {"math": math, "time": NS(monotonic=lambda: self.t)}
        exec(compile(ast.Module(body=[ramp, move], type_ignores=[]), "easing", "exec"), env)
        self.ramp = env["ApproachRamp"]
        self.move = env["_eased_move"]

    def test_starts_gently_reaches_cruise_and_eases_out(self):
        r = self.ramp()
        values = []
        for _ in range(120):
            self.t += 0.03
            values.append(r.step(0.18, 0.9))
        self.assertLess(values[0][0], 0.011)
        self.assertLess(values[0][1], 0.046)
        for a, b in zip(values, values[1:], strict=False):
            self.assertLessEqual(abs(b[0] - a[0]), 0.2 * 0.03 + 1e-9)
            self.assertLessEqual(abs(b[1] - a[1]), 0.8 * 0.03 + 1e-9)
        self.assertAlmostEqual(values[-1][0], 0.18, places=3)
        self.t += 0.03
        v, w = r.step(0, 0)
        self.assertTrue(0 < v < values[-1][0])
        self.assertTrue(0 < w < values[-1][1])

    def test_reversal_passes_through_zero_and_long_gap_does_not_jump(self):
        r = self.ramp()
        for _ in range(30):
            self.t += 0.03
            r.step(0.18, 0.9)
        before = r.vx
        self.t += 10
        r.step(-0.18, -0.9)
        self.assertGreaterEqual(r.vx, 0)
        self.assertLessEqual(before - r.vx, 0.2 * 0.06 + 1e-9)
        for _ in range(200):
            self.t += 0.03
            r.step(-0.18, -0.9)
        self.assertLess(r.vx, -0.17)

    def simulate(self, amount, turning=False, fail=None, params=None):
        position = [0.0, 0.0, 0.0]
        cmd = [0.0, 0.0]
        commands = []
        stops = []

        def send(v, w, d):
            cmd[:] = [v, w]
            commands.append((v, w))

        def sleep(dt):
            if fail == "cancel":
                raise RuntimeError("cancel")
            position[0] += cmd[0] * dt
            position[2] += cmd[1] * dt
            self.t += dt

        def odom():
            return None if fail == "odom" and self.t > 0.12 else tuple(position)

        s = NS(
            p={
                "rot_tol_deg": 2.5,
                "drive_tol_m": 0.003,
                "rot_kp": 2.4,
                "rot_wz_max": 0.9,
                "drive_kp": 0.9,
                "drive_v_max": 0.3,
            },
            name="test",
            odom_xyt=odom,
            host=NS(mobility=NS(send_cmd_vel=send, stop=lambda: stops.append(True)), sleep=sleep, logger=Mock()),
        )
        s.p.update(params or {})
        try:
            result = self.move(s, amount, turning=turning)
        finally:
            self.assertTrue(stops)
        return result, position, commands

    def test_short_forward_reverse_and_turn_complete(self):
        for amount, turn in [(0.02, False), (-0.15, False), (1.2, True), (-1.2, True)]:
            self.t = 0
            ok, pos, cmd = self.simulate(amount, turn)
            self.assertTrue(ok)
            self.assertLessEqual(abs(pos[2 if turn else 0] - amount), math.radians(2.5) if turn else 0.003)

    def test_faster_retreat_keeps_acceleration_and_arrival_bounds(self):
        old = {"drive_kp": 0.3, "drive_v_max": 0.1, "drive_tol_m": 0.015}
        self.simulate(-0.15, params=old)
        old_time = self.t
        self.t = 0
        ok, pos, commands = self.simulate(-0.15, params={**old, "drive_kp": 1.2, "drive_v_max": 0.2})
        self.assertTrue(ok)
        self.assertLess(self.t, old_time * 0.7)
        self.assertLessEqual(abs(pos[0] + 0.15), 0.015)
        self.assertLessEqual(max(abs(v) for v, _ in commands), 0.2)
        for (v0, _), (v1, _) in zip(commands, commands[1:]):
            self.assertLessEqual(abs(v1 - v0), 0.2 * 0.03 + 1e-9)

    def test_feedback_loss_and_cancel_stop_immediately(self):
        ok, _, _ = self.simulate(0.2, fail="odom")
        self.assertFalse(ok)
        self.t = 0
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            self.simulate(0.2, fail="cancel")


class TurnBrakingTests(unittest.TestCase):
    def test_gentle_turn_in_but_prompt_turn_out(self):
        clock = [0.0]
        node = next(
            n for n in ast.parse(P.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "ApproachRamp"
        )
        env = dict(math=math, time=NS(monotonic=lambda: clock[0]))
        exec(compile(ast.Module(body=[node], type_ignores=[]), "ramp", "exec"), env)
        r = env["ApproachRamp"]()
        previous = 0
        for _ in range(60):
            clock[0] += 0.02
            _, w = r.step(0, 0.35)
            self.assertLessEqual(w - previous, 0.8 * 0.02 + 1e-9)
            previous = w
        for _ in range(15):
            clock[0] += 0.02
            _, w = r.step(0, 0)
            self.assertLessEqual(previous - w, 2 * 0.02 + 1e-9)
            previous = w
        self.assertLess(w, 0.015)
