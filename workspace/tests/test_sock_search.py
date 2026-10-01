import ast
import math
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(__file__).parents[1]


class SockSearchTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "innate_skills/approach.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FloorApproach")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "search")
        env = {"math": math, "SkillFailed": RuntimeError}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "search", "exec"), env)
        self.search = env["search"]
        self.obj = NS(
            p={"search_turns_deg": (0, -90, -90, -90), "silent_search": True},
            host=NS(overlay=Mock(), say=Mock()),
            rotate_by=Mock(),
            _localize_px=Mock(),
        )

    def test_visible_sock_never_turns(self):
        self.obj._localize_px.return_value = ((0.3, 0), (320, 300))
        self.assertEqual(self.search(self.obj, "sock"), (0.3, 0))
        self.obj.rotate_by.assert_not_called()

    def test_search_stops_at_first_sock(self):
        self.obj._localize_px.side_effect = [(None, None), (None, None), ((0.3, 0), (320, 300))]
        self.assertEqual(self.search(self.obj, "sock"), (0.3, 0))
        self.assertEqual(self.obj.rotate_by.call_count, 2)
        self.obj.host.say.assert_not_called()
        self.assertTrue(all(c.args == (-math.pi / 2,) for c in self.obj.rotate_by.call_args_list))

    def test_empty_search_is_bounded(self):
        self.obj._localize_px.return_value = (None, None)
        with self.assertRaises(RuntimeError):
            self.search(self.obj, "sock")
        self.assertEqual(self.obj._localize_px.call_count, 4)
        self.assertEqual(self.obj.rotate_by.call_count, 3)

    def test_cancel_propagates_without_more_detection(self):
        self.obj._localize_px.return_value = (None, None)
        self.obj.rotate_by.side_effect = InterruptedError()
        with self.assertRaises(InterruptedError):
            self.search(self.obj, "sock")
        self.assertEqual(self.obj._localize_px.call_count, 1)

    def test_existing_search_keeps_original_turns(self):
        self.obj.p = {}
        self.obj._localize_px.return_value = (None, None)
        with self.assertRaises(RuntimeError):
            self.search(self.obj, "sock")
        self.assertEqual([c.args[0] for c in self.obj.rotate_by.call_args_list],
                         [-math.pi / 6, math.pi / 3])
        self.obj.host.say.assert_called_once()

    def test_agent_exposes_pick_drop_wave_and_microphone(self):
        tree = ast.parse((ROOT / "innate_agents/qwen_sock_demo_2.py").read_text())
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        pick, drop, wave, mic = object(), object(), object(), object()
        env = {"QwenSockAgent": object, "PickSockYoloe": pick, "DropInBoxAruco": drop, "Wave": wave, "MicroInput": mic}
        exec(compile(tree, "agent", "exec"), env)
        agent = env["QwenSockDemo2"]()
        self.assertEqual(agent.get_skills(), [pick, drop, wave])
        self.assertEqual(agent.get_inputs(), [mic])
        self.assertEqual(agent.display_name, "Qwen Sock Demo 2")
