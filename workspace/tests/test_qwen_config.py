import ast
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

R = Path(__file__).resolve().parents[2]


class EffortTests(unittest.TestCase):
    def test_switch_and_rollback(self):
        t = ast.parse((R / "ros2_ws/src/brain/brain_client/brain_client/brain/agent.py").read_text())
        method = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == "use_model")
        env = {"DEFAULT_MODEL": "astra"}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "agent", "exec"), env)
        a = NS(
            _agent_spec=None,
            _default_spec="astra",
            _agent_extra_body=None,
            _reconfigure=Mock(return_value=(True, "ok")),
        )

        def switch(spec, **kw):
            return env["use_model"](a, spec, **kw)

        switch("qwen", agent=True, model_extra_body="{}")
        self.assertEqual(a._agent_extra_body, "{}")
        a._reconfigure.assert_called_with("qwen", force=True)
        switch("astra-new", agent=False)
        self.assertEqual(a._agent_extra_body, "{}")
        switch("qwen", agent=True, model_extra_body='{"new":true}')
        a._reconfigure.assert_called_with("qwen", force=True)
        a._reconfigure.return_value = (False, "bad")
        switch(None, agent=True)
        self.assertEqual(a._agent_extra_body, '{"new":true}')
        self.assertEqual(a._agent_spec, "qwen")
        a._reconfigure.return_value = (True, "ok")
        switch(None, agent=True)
        self.assertIsNone(a._agent_extra_body)
        self.assertEqual(a._default_spec, "astra-new")

    def test_skill_config_and_agent_surface(self):
        env = {
            "SkillReturn": str,
            "json": __import__("json"),
            "math": __import__("math"),
            "Llm": lambda *a, **kw: NS(model=a[0], **kw),
            "PickSockFast": type("PickSockFast", (), {}),
        }
        t = ast.parse((R / "workspace/innate_skills/pick_sock_qwen.py").read_text())
        t.body = [n for n in t.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        exec(compile(t, "skill", "exec"), env)
        q = env["PickSockQwen"]
        self.assertFalse(__import__("json").loads(q.llm.extra_body)["chat_template_kwargs"]["enable_thinking"])
        self.assertFalse(hasattr(q.llm, "base_url"))
        base = env["PickSockFast"]
        other = object()
        env["SockRehearsedAgent"] = type("A", (), {"get_skills": lambda self: [base, other]})
        t = ast.parse((R / "workspace/innate_agents/qwen_sock_agent.py").read_text())
        t.body = [n for n in t.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        exec(compile(t, "agent", "exec"), env)
        a = env["QwenSockAgent"]()
        self.assertEqual(a.get_skills(), [q, other])
        self.assertIn('enable_thinking":false', a.model_extra_body)

    def test_described_skill_target(self):
        import json

        env = dict(
            json=json,
            Llm=lambda *a, **kw: None,
            SkillReturn=str,
            PickSockFast=type("Base", (), {"execute": lambda self, prompt: prompt}),
        )
        t = ast.parse((R / "workspace/innate_skills/pick_sock_qwen.py").read_text())
        t.body = [n for n in t.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        exec(compile(t, "qwen", "exec"), env)
        skill = env["PickSockQwen"]()
        self.assertEqual(skill.execute("the blue sock"), "the blue sock")
        skill._target_description = "the blue sock"
        self.assertIn("the blue sock", skill._detection_question("all matching socks"))

    def test_sock_identity_never_reanchors_after_repeated_misses(self):
        t = ast.parse((R / "workspace/innate_skills/pick_any_object.py").read_text())
        fn = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == "_choose_cand")
        env = {"MEM_COAST_LIMIT": 3}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "identity", "exec"), env)
        host = NS(
            _last_seen=(1, 2, 0.5),
            _coasts=0,
            logger=Mock(),
            _p={"lock_target_identity": True, "mem_gate_m": 0.12, "mem_gate_frac": 0},
            _memory_dist=lambda candidate: candidate,
        )
        for _ in range(10):
            self.assertIsNone(env["_choose_cand"](host, [0.3, 0.5]))
            self.assertEqual(host._last_seen, (1, 2, 0.5))
        self.assertEqual(env["_choose_cand"](host, [0.3, 0.04]), 0.04)

    def test_boxes_validate_and_ignore_swapped_grasp_point(self):
        import json
        import math
        import re

        env = dict(json=json, math=math, re=re, IMG_W=640, IMG_H=480)
        t = ast.parse((R / "ros2_ws/src/brain/brain_client/innate/vision.py").read_text())
        fns = [
            n
            for n in t.body
            if isinstance(n, ast.FunctionDef)
            and n.name
            in ("parse_dets", "_norm1k", "_box_corners_px", "_grasp_px", "_center_px", "_grip", "parse_det_cands_boxed")
        ]
        exec(compile(ast.Module(body=fns, type_ignores=[]), "vision", "exec"), env)
        vision = NS(**{n.name: env[n.name] for n in fns})
        t = ast.parse((R / "workspace/innate_skills/pick_sock_qwen.py").read_text())
        fn = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == "_parse_detections")
        env.update(vision=vision)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "qwen", "exec"), env)

        def parse(text):
            return env["_parse_detections"](NS(logger=Mock()), text)

        got = parse('[{"box_2d":[800,280,880,340],"grasp_point":[310,840]}]')
        self.assertAlmostEqual(got[0][0], 198.4)
        self.assertAlmostEqual(got[0][1], 403.2)
        named = '{"detections":[{"x_min":280,"y_min":800,"x_max":340,"y_max":880}]}'
        self.assertEqual(parse(named), got)
        self.assertEqual(parse("```json\n" + named + "\n```"), got)
        self.assertEqual(parse('[{"bbox_2d":[280,800,340,880],"coordinate_order":"xyxy"}]'), got)
        self.assertEqual(parse('[{"bbox_2d":[800,280,880,340],"coordinate_order":"yxyx"}]'), got)
        for text in (
            "[]",
            "no socks",
            '{"detections":[]}',
            '[{"bbox_2d":[558,558,590,602],"label":"sock"}]',
            '{"detections":"bad"}',
            '{"detections":[[]]}',
            '{"detections":[{"x_min":0,"y_min":0,"x_max":true,"y_max":100}]}',
            '{"detections":[{"x_min":0,"y_min":0,"x_max":1001,"y_max":100}]}',
            '{"detections":[{"x_min":0,"y_min":0,"x_max":100,"y_max":100}',
            '[{"box_2d":[-1,0,20,30]}]',
            '[{"box_2d":[20,0,10,30]}]',
            '[{"box_2d":[true,0,20,30]}]',
            '[{"box_2d":[0,0,null,30]}]',
        ):
            self.assertEqual(parse(text), [])


if __name__ == "__main__":
    unittest.main()
