"""What each agent is given, and what it is charged for, against what review found.

Two cups shared one memory key; the Codex and stack backends were told about
a wrist image they were never sent; and a narrator line during the last skill
bought a model call past the turn budget.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import base64
import json
import types

import pytest
from backends import CodexBackend, EchoBackend
from backends_v2 import NemotronStackBackend
from brain_agent import BrainAgent, Observation


class _Mars:
    def pose(self):
        return (0.0, 0.0, 0.0)


def _held(agent: BrainAgent, name: str | None) -> Observation:
    agent._carrying = name
    return agent._observe(_Mars(), 1.0)


def test_two_cups_get_two_handles_and_keep_them() -> None:
    agent = BrainAgent(EchoBackend())
    red, blue = _held(agent, "counter_cup_red"), _held(agent, "counter_cup_blue")
    assert red.carrying == blue.carrying  # the model sees the kind, as before
    assert red.carrying_id != blue.carrying_id
    assert _held(agent, "counter_cup_red").carrying_id == red.carrying_id
    assert "counter_cup" not in red.carrying_id  # no simulator names
    assert _held(agent, None).carrying_id is None


def test_releasing_the_second_cup_keeps_the_first_cups_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    backend = NemotronStackBackend()
    backend._call = lambda *a, **k: {"action": "wait", "args": {}}  # type: ignore[method-assign]
    agent = BrainAgent(EchoBackend())
    for name in ("counter_cup_red", None, "counter_cup_blue", None):
        backend.decide(_held(agent, name), "menu")
    released = [k for k in backend.stack.facts if k.startswith("released:")]
    assert len(released) == 2, backend.stack.facts


def _obs_with_frames(tmp_path) -> Observation:
    head, wrist = tmp_path / "head.jpg", tmp_path / "wrist.jpg"
    head.write_bytes(b"HEAD")
    wrist.write_bytes(b"WRIST")
    return Observation(brief="b", elapsed_s=0.0, image_path=str(head), wrist_image_path=str(wrist))


def _sent_images(monkeypatch: pytest.MonkeyPatch, backend: NemotronStackBackend, obs, **kw) -> list[bytes]:
    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": '{"action":"wait"}'}]}}]}).encode()

    def urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data)
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    backend._call("sys", obs, "text", want_image=True, **kw)
    parts = sent["body"]["contents"][0]["parts"]
    return [base64.b64decode(p["inline_data"]["data"]) for p in parts if "inline_data" in p]


def test_the_stack_backend_decides_with_both_cameras(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    backend = NemotronStackBackend()
    obs = _obs_with_frames(tmp_path)
    assert _sent_images(monkeypatch, backend, obs, wrist=True) == [b"HEAD", b"WRIST"]
    # Grounding locates a point in the head frame, so it gets that alone.
    assert _sent_images(monkeypatch, backend, obs) == [b"HEAD"]


def test_codex_is_told_where_the_wrist_frame_is(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/codex")
    prompts = []

    def run(cmd, input=None, **kw):  # noqa: A002
        prompts.append(input)
        return types.SimpleNamespace(stdout='{"action": "wait", "args": {}}', stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", run)
    obs = _obs_with_frames(tmp_path)
    CodexBackend().decide(obs, "menu")
    assert obs.image_path in prompts[0] and obs.wrist_image_path in prompts[0]


def test_speech_during_the_last_skill_buys_no_extra_turn() -> None:
    agent = BrainAgent(EchoBackend(), max_turns=3)
    agent.turns = 3
    agent._skill = ("navigate_to_position", iter(()), 0.0)
    agent._step_skill = lambda mars, t: None  # type: ignore[method-assign]
    calls = []
    agent._start_call = lambda mars, t: calls.append(t)  # type: ignore[method-assign]
    agent._heard = ["Hello?"]
    agent.act(object(), 5.0)
    assert calls == []
    agent.turns = 2
    agent.act(object(), 6.0)
    assert calls == [6.0]  # under budget, speech still wakes it
