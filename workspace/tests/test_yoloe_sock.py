import ast
import base64
import io
import json
import math
import os
import time
import unittest
import urllib
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import Mock, patch


class YoloeTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / "innate_skills/pick_sock_yoloe.py"
        tree = ast.parse(path.read_text())
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        parent = type("Parent", (), {"_p": {"settle_s": 0.6}})
        env = dict(base64=base64, json=json, math=math, os=os, time=time, urllib=urllib, uuid=uuid,
                   PickSockFast=parent, SkillReturn=str, SkillFailed=RuntimeError,
                   IMG_W=640, IMG_H=480, settled_frame=lambda *a: base64.b64encode(b"jpeg").decode())
        exec(compile(tree, str(path), "exec"), env)
        self.parse = env["detection_candidates"]
        self.skill = env["PickSockYoloe"]()
        self.skill.mobility = Mock()
        self.skill.check_cancelled = Mock()
        self.skill.logger = Mock()
        self.payload = {"image": {"width": 1280, "height": 960},
                        "detections": [{"confidence": 0.2, "bbox_xyxy": [200, 400, 600, 800]}]}

    def test_scales_boxes_to_camera_and_rejects_bad_values(self):
        self.assertEqual(self.parse(self.payload), [(200, 300, None, (100, 200, 300, 400))])
        self.payload["detections"][0]["bbox_xyxy"][0] = float("nan")
        with self.assertRaises(ValueError):
            self.parse(self.payload)

    def test_empty_and_low_confidence(self):
        self.payload["detections"][0]["confidence"] = 0.01
        self.assertEqual(self.parse(self.payload), [])
        self.payload["detections"] = []
        self.assertEqual(self.parse(self.payload), [])

    def test_request_sends_green_prompt_without_llm(self):
        with patch.object(urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(self.payload).encode())) as send:
            candidates, _ = self.skill._detect_candidates("green sock")
        self.assertEqual(len(candidates), 1)
        self.assertIn(b'name="prompt"\r\n\r\ngreen sock', send.call_args.args[0].data)
        self.assertIn(b"jpeg", send.call_args.args[0].data)
        self.assertFalse(self.skill.requires_llm)

    def test_outage_fails_without_fallback(self):
        with patch.object(urllib.request, "urlopen", side_effect=TimeoutError("timeout")):
            with self.assertRaisesRegex(RuntimeError, "YOLOE detection failed"):
                self.skill._detect_candidates("green sock")

    def test_cancel_before_request_sends_nothing(self):
        self.skill.check_cancelled.side_effect = InterruptedError()
        with patch.object(urllib.request, "urlopen") as send:
            with self.assertRaises(InterruptedError):
                self.skill._detect_candidates("green sock")
            send.assert_not_called()

    def test_pile_prompt_replaces_green_sock_in_wire_request(self):
        self.skill.detection_prompt = "a pile of socks"
        with patch.object(urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(self.payload).encode())) as send:
            self.skill._detect_candidates("a pile of socks")
        body = send.call_args.args[0].data
        self.assertIn(b'name="prompt"\r\n\r\na pile of socks', body)
        self.assertNotIn(b"green sock", body)

    def test_pile_prefers_largest_only_before_identity_lock(self):
        path = Path(__file__).parents[1] / "innate_skills/pick_sock_pile_yoloe.py"
        tree = ast.parse(path.read_text())
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        parent = type("Parent", (), {"_choose_cand": lambda self, c: c[0]})
        env = {"PickSockYoloe": parent, "SkillReturn": str}
        exec(compile(tree, str(path), "exec"), env)
        skill = env["PickSockPileYoloe"]()
        small = (1, 1, None, (0, 0, 2, 2))
        big = (10, 10, None, (0, 0, 20, 20))
        skill._last_seen = None
        self.assertEqual(skill._choose_cand([small, big]), big)
        skill._last_seen = (1, 1, 1)
        self.assertEqual(skill._choose_cand([small, big]), small)
        self.assertEqual(skill.detection_prompt, "a pile of socks")
