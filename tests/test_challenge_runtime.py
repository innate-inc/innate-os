# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Focused checks for generic simulated-character challenge infrastructure."""

import json
import math
import socket
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PACKAGE = REPO_ROOT / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"

try:
    import mujoco  # noqa: F401
except ImportError:
    fake_mujoco = types.ModuleType("mujoco")
    fake_mujoco.MjModel = object
    fake_mujoco.MjSpec = object
    sys.modules["mujoco"] = fake_mujoco

sys.path.insert(0, str(DRIVER_PACKAGE))

from mars_sim_driver.challenges import (  # noqa: E402
    Challenge,
    ChallengeChatBridge,
    ChallengeEngine,
    ChallengeRuntime,
    EnvironmentReply,
    EventSeen,
    Goal,
    RuntimeResult,
    load_challenges,
)
from mars_sim_driver.props import Prop, PropRegistry  # noqa: E402


class ReplyRuntime(ChallengeRuntime):
    def reset(self) -> None:
        self.reset_count = getattr(self, "reset_count", 0) + 1

    def update(self, _state, events) -> RuntimeResult:
        result = RuntimeResult()
        for event in events:
            if event.get("type") != "robot_speech":
                continue
            resident = event["text"]
            result.events.append({"type": "confirmed", "resident": resident})
            result.replies.append(EnvironmentReply(resident.title(), "Confirmed", f"voice-{resident}"))
        return result


def _engine(tmp_path: Path):
    sim = SimpleNamespace(data=SimpleNamespace(time=0.0))
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=tmp_path / "progress.json")
    runtime = ReplyRuntime()
    challenge = Challenge(
        id="characters",
        title="Characters",
        brief="Talk to both characters.",
        setup=[],
        goals=[
            Goal("Confirm A", EventSeen("confirmed", {"resident": "a"}), parallel_group="people"),
            Goal("Confirm B", EventSeen("confirmed", {"resident": "b"}), parallel_group="people"),
        ],
        runtime=runtime,
        reset_world=False,
    )
    engine.challenges = {challenge.id: challenge}
    assert engine.start(challenge.id)
    return engine, sim, runtime


def _tick(engine: ChallengeEngine, sim) -> dict:
    sim.data.time += 0.1
    return engine.tick(sim.data.time, (0.0, 0.0, 0.0), {}, engine.world_epoch)


def test_loader_supports_challenge_packages_with_private_modules(tmp_path):
    package = tmp_path / "20_characters"
    package.mkdir()
    (package / "runtime.py").write_text("TITLE = 'Packaged challenge'\n")
    (package / "__init__.py").write_text(
        "from mars_sim_driver.challenges import Challenge\n"
        "from .runtime import TITLE\n"
        "CHALLENGE = Challenge(id='packaged', title=TITLE, brief='brief', setup=[], goals=[])\n"
    )

    loaded = load_challenges([tmp_path])

    assert loaded["packaged"].title == "Packaged challenge"


def test_runtime_events_are_trusted_and_parallel_goals_are_unordered(tmp_path):
    engine, sim, runtime = _engine(tmp_path)
    assert runtime.reset_count == 1

    engine.post_event({"type": "confirmed", "resident": "a"})
    block = _tick(engine, sim)
    assert [goal["done"] for goal in block["active"]["goals"]] == [False, False]

    engine.post_robot_speech("b")
    block = _tick(engine, sim)
    assert [goal["done"] for goal in block["active"]["goals"]] == [False, True]

    engine.post_robot_speech("a")
    block = _tick(engine, sim)
    assert block["active"]["state"] == "passed"
    assert [goal["done"] for goal in block["active"]["goals"]] == [True, True]


def test_environment_reply_is_a_speech_request_the_brain_voices(tmp_path):
    engine, sim, _runtime = _engine(tmp_path)
    engine.post_robot_speech("a")
    _tick(engine, sim)

    token, payload = engine.next_chat_input(timeout=0.0)

    assert engine.chat_input_is_current(token)
    assert payload["text"] == "Confirmed"
    assert payload["speaker"] == "A"
    assert payload["sender"] == "environment_speech"
    assert payload["voice_id"] == "voice-a"


def test_restart_invalidates_queued_and_dequeued_replies(tmp_path):
    engine, sim, _runtime = _engine(tmp_path)
    engine.post_robot_speech("a")
    _tick(engine, sim)
    old_token, _old_payload = engine.next_chat_input(timeout=0.0)

    assert engine.start("characters")

    assert not engine.chat_input_is_current(old_token)
    assert engine.next_chat_input(timeout=0.0) is None


def _topic_frame(topic: str, payload: dict) -> str:
    return json.dumps({"topic": topic, "msg": {"data": json.dumps(payload)}})


def test_chat_bridge_accepts_only_visible_robot_speech():
    robot = _topic_frame(
        ChallengeChatBridge.CHAT_OUT,
        {"sender": "robot", "text": "Hello", "timestamp": 42.5},
    )
    thought = _topic_frame(
        ChallengeChatBridge.CHAT_OUT,
        {"sender": "robot_thoughts", "text": "private", "timestamp": 42.5},
    )
    assert ChallengeChatBridge.robot_speech(robot) == ("Hello", 42.5)
    assert ChallengeChatBridge.robot_speech(thought) is None


def test_chat_write_timeout_interrupts_a_stalled_socket():
    released = threading.Event()
    shutdown_calls = []

    class Connection:
        socket = SimpleNamespace(shutdown=lambda how: (shutdown_calls.append(how), released.set()))

        @staticmethod
        def send(_message):
            assert released.wait(1.0)
            raise OSError("socket closed")

    with pytest.raises(TimeoutError):
        ChallengeChatBridge._send_with_timeout(Connection(), "frame", 0.02)

    assert shutdown_calls == [socket.SHUT_RDWR]


def test_chat_write_that_completes_at_deadline_is_not_retried():
    released = threading.Event()

    class Connection:
        socket = SimpleNamespace(shutdown=lambda _how: released.set())

        @staticmethod
        def send(_message):
            assert released.wait(1.0)

    ChallengeChatBridge._send_with_timeout(Connection(), "frame", 0.02)


def test_kinematic_props_use_mocap_pose_without_a_freejoint():
    prop = Prop(name="character", kinematic=True, rest_z=0.0)
    registry = PropRegistry({prop.name: prop})

    assert 'mocap="true"' in registry.bodies_xml(visual_group=2, collision_group=3)
    assert "freejoint" not in registry.bodies_xml(visual_group=2, collision_group=3)

    model = SimpleNamespace(body=lambda _name: SimpleNamespace(id=0), body_mocapid=[0])
    data = SimpleNamespace(
        mocap_pos=[[0.0, 0.0, 0.0]],
        mocap_quat=[[1.0, 0.0, 0.0, 0.0]],
    )
    registry.bind(model)

    assert registry.drop_at(data, prop.name, 1.0, 2.0, math.pi / 2)
    assert data.mocap_pos[0] == [1.0, 2.0, 0.0]
    assert data.mocap_quat[0] == pytest.approx([math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)])
