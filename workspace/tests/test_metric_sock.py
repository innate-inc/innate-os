import ast
import math
import unittest
from types import SimpleNamespace as NS

from test_detection_recovery import FollowRecoveryTests
from test_shortcut_activation import functions


class MetricFollowTests(FollowRecoveryTests):
    def setUp(self):
        super().setUp()
        helpers = functions("sock_arrived", "sock_approach_speed", "sock_approach_turn", "approach_linear_limits")
        self.follow.__globals__.update(
            {
                k: helpers[k]
                for k in ("sock_arrived", "sock_approach_speed", "sock_approach_turn", "approach_linear_limits")
            }
        )
        self.follow.__globals__.update(STATIC_MIN_PX=2, _min_px_shift=lambda *a: 0)
        self.s.p.update(
            metric_sock_approach=True,
            sweet_x=0.315,
            follow_gain_ang=0.3,
            rot_wz_min=0.2,
            rot_wz_max=0.9,
            follow_gain_lin=0.12,
            drive_v_min=0.04,
            drive_v_max=0.1,
        )
        self.host.mobility.servo_vel.return_value = 0

    def test_old_pixel_stop_keeps_driving_until_metric_arrival(self):
        self.tracker.track.side_effect = [(358, 10), (340, 10), (332, 10), (332, 10), (332, 10)]
        result = self.follow(self.s, (358, 10))
        self.assertEqual(result, ("in_box", (332, 10)))
        self.assertEqual(self.host.mobility.send_cmd_vel.call_count, 2)
        self.assertGreater(self.host.mobility.send_cmd_vel.call_args_list[0].args[0], 0)
        self.assertEqual(len(self.s._arrival_samples), 3)

    def test_invalid_projection_stops(self):
        self.tracker.track.return_value = (float("nan"), 10)
        self.assertEqual(self.follow(self.s, (315, 10)), ("lost", None))
        self.host.mobility.send_cmd_vel.assert_not_called()


class BrakingTests(unittest.TestCase):
    def test_distance_braking_and_limits(self):
        f = functions("sock_approach_speed")["sock_approach_speed"]
        values = [f((x, 0), 0.315) for x in [0.8, 0.5, 0.4, 0.36, 0.34, 0.33]]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(values[0], 0.18)
        self.assertGreater(values[3], 0.025)
        self.assertEqual(values[-1], 0)
        self.assertGreater(f((0.8, 0.1), 0.315), 0.16)
        self.assertEqual(f((0.2, 0.5), 0.315), 0)
        self.assertGreaterEqual(f((0.2, 0), 0.315), -0.06)
        self.assertEqual(f((float("nan"), 0), 0.315), 0)

    def test_ramp_simulation_reaches_without_overshoot(self):
        f = functions("sock_approach_speed", "sock_arrived")
        tree = ast.parse(__import__("test_approach_easing").P.read_text())
        ramp = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ApproachRamp")
        t = [0.0]
        env = dict(math=math, time=NS(monotonic=lambda: t[0]))
        exec(compile(ast.Module(body=[ramp], type_ignores=[]), "ramp", "exec"), env)
        for initial in [0.358, 0.45, 0.823]:
            r = env["ApproachRamp"]()
            x = initial
            prev = 0
            for _ in range(600):
                if f["sock_arrived"]((x, 0), 0.315):
                    break
                t[0] += 0.03
                v, _ = r.step(f["sock_approach_speed"]((x, 0), 0.315), 0)
                self.assertLessEqual(abs(v - prev), 0.2 * 0.03 + 1e-8)
                x -= v * 0.03
                prev = v
            self.assertTrue(f["sock_arrived"]((x, 0), 0.315))
            self.assertGreater(x, 0.315)


class SockSteeringTests(unittest.TestCase):
    def test_steering_is_symmetric_and_has_no_minimum_turn_kick(self):
        f = functions("sock_approach_turn")["sock_approach_turn"]
        self.assertEqual(f((1, 0.04), 0.315), 0)
        self.assertGreater(f((1, 0.1), 0.315), 0)
        self.assertLess(f((1, 0.1), 0.315), 0.08)
        self.assertAlmostEqual(f((1, 0.1), 0.315), -f((1, -0.1), 0.315))
        self.assertGreater(f((0.34, 0.035), 0.315), 0)
        self.assertEqual(f((0.34, 0.01), 0.315), 0)

    def test_curved_approach_reaches_with_gentle_acceleration(self):
        helpers = functions("sock_approach_speed", "sock_approach_turn", "sock_arrived")
        tree = ast.parse(__import__("test_approach_easing").P.read_text())
        ramp = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ApproachRamp")
        for start in [(1.071, -0.101), (1.071, 0.101), (0.7, 0.3), (0.36, 0.06)]:
            clock = [0.0]
            env = dict(math=math, time=NS(monotonic=lambda clock=clock: clock[0]))
            exec(compile(ast.Module(body=[ramp], type_ignores=[]), "ramp", "exec"), env)
            r = env["ApproachRamp"]()
            x, y = start
            previous = 0
            for _ in range(650):
                if helpers["sock_arrived"]((x, y), 0.315):
                    break
                clock[0] += 0.03
                vx, wz = r.step(
                    helpers["sock_approach_speed"]((x, y), 0.315), helpers["sock_approach_turn"]((x, y), 0.315)
                )
                self.assertLessEqual(abs(vx - previous), 0.2 * 0.03 + 1e-8)
                previous = vx
                x, y = x + (-vx + wz * y) * 0.03, y - wz * x * 0.03
            self.assertTrue(helpers["sock_arrived"]((x, y), 0.315), (start, x, y))
            self.assertGreater(x, 0.28)
