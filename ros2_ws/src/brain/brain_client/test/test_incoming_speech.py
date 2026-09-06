# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Incoming speech spans queueing, playback, and transcript delivery; no paid calls."""

import asyncio
import io
import json
import threading
import time
import wave
from collections import deque
from types import SimpleNamespace

import pytest
import test_local_brain
from test_local_brain import NAV_SKILL, call_part, model_response, run_turn
from test_openai_context import call_item, completed, context

from brain_client.brain.context import ToolCall
from brain_client.brain.tools import STOP_SKILL, WAIT
from brain_client.brain.utils import Event, EventKind
from brain_client.core.state import RunningSkill
from brain_client.skills.registry import SkillRegistry

IDENTITY = {"id": "innate-os/person_identity", "name": "person_identity", "inputs": {}}
agent_factory = test_local_brain.agent_factory


def enable_skills(agent, state):
    state.registry = SkillRegistry.from_metadata([NAV_SKILL, IDENTITY])
    agent._roster.active_skill_ids = lambda: [NAV_SKILL["id"], IDENTITY["id"]]
    started = []
    agent._runner.start_task = lambda *args: started.append(args)
    return started


@pytest.mark.parametrize("provider", ["gemini", "openai"])
@pytest.mark.parametrize("complete_before_return", [False, True])
def test_inflight_calls_are_fenced_and_all_provider_outcomes_complete(agent_factory, provider, complete_before_return):
    agent, state = agent_factory()
    started = enable_skills(agent, state)
    calls = [("navigate_to_position", {"x": 1, "y": 0}), ("person_identity", {}), (WAIT, {})]
    token = None
    requests = []

    def transport(model, body):
        nonlocal token
        requests.append(body)
        if len(requests) > 1:
            yield completed(call_item("next")) if provider == "openai" else model_response(call_part(WAIT, {}))
            return
        token = agent.begin_incoming_speech()
        if complete_before_return:
            agent.finish_incoming_speech(token, "A complete resident reply")
        if provider == "openai":
            yield completed(*(call_item(str(i), name, json.dumps(args)) for i, (name, args) in enumerate(calls)))
        else:
            yield model_response(*(call_part(name, args, str(i)) for i, (name, args) in enumerate(calls)))

    if provider == "openai":
        agent._context = context(transport)
    else:
        agent._context._transport = transport
    run_turn(agent)
    assert started == []
    history = agent._context._history
    outcomes = [p["functionResponse"] for turn in history for p in turn["parts"] if "functionResponse" in p]
    assert len(outcomes) == 3
    assert all("rejected" in json.dumps(item) for item in outcomes[:2])
    assert "ok" in json.dumps(outcomes[2])
    if not complete_before_return:
        agent.finish_incoming_speech(token, "A complete resident reply")
    assert any(event.kind == EventKind.USER for event in agent._events)
    run_turn(agent)
    if provider == "openai":
        replies = [item for item in requests[1]["input"] if item.get("type") == "function_call_output"]
        assert {item["call_id"] for item in replies} == {"0", "1", "2"}
    else:
        replies = [
            part["functionResponse"]
            for item in requests[1]["contents"]
            for part in item["parts"]
            if "functionResponse" in part
        ]
        assert {item["id"] for item in replies} == {"0", "1", "2"}


def test_overlap_duplicates_and_reset_do_not_reopen_or_resurrect_input(agent_factory):
    agent, state = agent_factory()
    started = enable_skills(agent, state)
    first, second = agent.begin_incoming_speech(), agent.begin_incoming_speech()
    agent.finish_incoming_speech(first, "First")
    agent.finish_incoming_speech(first, "Duplicate")
    assert [d["name"] for d in agent._build_tools([])[0]["functionDeclarations"]] == [WAIT]
    assert "rejected" in agent._execute(ToolCall("person_identity", {}))
    assert started == []
    assert [e.text for e in agent._events if e.kind == EventKind.USER] == ['The user says: "First"']
    agent.reset()
    agent.finish_incoming_speech(second, "Old reply after reset")
    assert agent._incoming_speech == set() and agent._events == []


def test_input_keeps_manual_skills_and_own_speech_ungated(agent_factory):
    agent, state = agent_factory()
    enable_skills(agent, state)
    cancelled = []
    agent._runner.interrupt_for_input = lambda ids: cancelled.append(ids)
    own = agent._chat.stream_speech()
    own.feed("My own reply. ")
    assert not agent._incoming_speech
    assert {d["name"] for d in agent._build_tools([])[0]["functionDeclarations"]} >= {"person_identity", WAIT}
    state.primitive_running = RunningSkill("navigate_to_position", NAV_SKILL["id"], manual=True)
    token = agent.begin_incoming_speech()
    assert cancelled == []
    assert {d["name"] for d in agent._build_tools([])[0]["functionDeclarations"]} == {WAIT, STOP_SKILL}
    assert "manual" in agent._execute(ToolCall(STOP_SKILL, {}))
    # A real user Stop still follows the ordinary authorized cancellation path.
    agent._runner.has_active_goal = False
    agent._runner.cancel_external = lambda: cancelled.append("manual stop") or True
    agent._build_tools([Event('The user says: "Stop now"', kind=EventKind.USER)])
    assert "stopping" in agent._execute(ToolCall(STOP_SKILL, {}))
    assert cancelled == ["manual stop"]
    state.is_brain_active = False  # explicit Stop wins over a later completion
    assert agent.stop()
    state.is_brain_active = True
    agent.finish_incoming_speech(token, "Late transcript after reactivation")
    assert agent._events == [] and not agent._incoming_speech


def test_fresh_turn_cannot_snapshot_between_transcript_queue_and_gate_retirement(agent_factory):
    agent, _ = agent_factory()
    token = agent.begin_incoming_speech()
    enqueue_started, release_enqueue, model_started = threading.Event(), threading.Event(), threading.Event()
    original = agent.on_user_message

    def enqueue(text):
        enqueue_started.set()
        assert release_enqueue.wait(3)
        original(text)

    agent.on_user_message = enqueue
    requests = []

    def transport(model, body):
        requests.append(body)
        model_started.set()
        yield model_response(call_part(WAIT, {}))

    agent._context._transport = transport
    finish = threading.Thread(target=agent.finish_incoming_speech, args=(token, "Completed transcript"))
    finish.start()
    assert enqueue_started.wait(3)
    turn = asyncio.run_coroutine_threadsafe(agent._turn(agent._context), agent._runtime.loop)
    try:
        assert not model_started.wait(0.05)
    finally:
        release_enqueue.set()
        finish.join(3)
    turn.result(timeout=3)
    assert "Completed transcript" in json.dumps(requests[0])
    assert agent._events == [] and not agent._incoming_speech


def test_committed_user_stop_can_cancel_manual_run_across_new_input_generation(agent_factory):
    agent, state = agent_factory()
    state.primitive_running = RunningSkill("navigate_to_position", NAV_SKILL["id"], manual=True)
    stopped = []
    agent._runner.has_active_goal = False
    agent._runner.cancel_external = lambda: stopped.append(True) or True

    def transport(*args):
        agent.begin_incoming_speech()  # new speech cannot revoke the user's Stop
        yield model_response(call_part(STOP_SKILL, {}))

    agent._context._transport = transport
    agent.on_user_message("Stop now")
    run_turn(agent)
    assert stopped == [True]


def test_stale_streamed_reply_is_not_published_as_complete_dialogue(agent_factory):
    agent, _ = agent_factory()
    published = []
    agent._chat.emit = lambda *args, **kwargs: published.append(args)

    def transport(*args):
        yield model_response({"text": "First sentence. "})
        agent.begin_incoming_speech()
        yield model_response({"text": "Stale continuation. "}, call_part(WAIT, {}))

    agent._context._transport = transport
    run_turn(agent)
    assert agent._chat.spoken == [("First sentence.", True)]
    assert published == []
    assert any("previous reply was interrupted" in e.text for e in agent._events)


def test_input_beginning_during_thought_publication_fences_final_speech_flush(agent_factory):
    agent, _ = agent_factory()
    published = []
    agent._chat.emit = lambda *args, **kwargs: published.append(args)
    agent._chat.emit_thoughts = lambda _: agent.begin_incoming_speech()
    agent._context._transport = lambda *_: [
        model_response(
            {"text": "Thinking", "thought": True},
            {"text": "Buffered reply without sentence boundary"},
            call_part(WAIT, {}),
        )
    ]
    run_turn(agent)
    assert agent._incoming_speech
    assert agent._chat.spoken == [] and published == []
    assert any("previous reply was interrupted" in e.text for e in agent._events)


@pytest.fixture
def speech_node(agent_factory):
    pytest.importorskip("rclpy")
    from brain_client.nodes.brain_client_node import BrainClientNode

    agent, state = agent_factory()
    started = enable_skills(agent, state)
    node = SimpleNamespace(
        config=SimpleNamespace(simulator_mode=True),
        brain=agent,
        state=state,
        chat=agent._chat,
        _tts_handler=None,
        get_logger=lambda: SimpleNamespace(warning=lambda *a: None, error=lambda *a: None),
    )
    return (
        node,
        lambda: BrainClientNode._on_environment_speech(node, {"text": "Resident reply", "voice_id": "v"}),
        started,
    )


def make_tts():
    from brain_client.transport.tts import TTSHandler

    handler = TTSHandler.__new__(TTSHandler)
    handler._speech_queue = deque()
    handler._speech_queue_maxlen = 16
    handler._speech_cv = threading.Condition()
    handler._playing_reply_id = None
    handler._closing = threading.Event()
    handler._cartesia_client = None
    handler.logger = SimpleNamespace(**{name: lambda *a: None for name in ("info", "error", "warning", "debug")})
    handler.is_available = lambda: True
    return handler


def test_real_queue_holds_for_twelve_second_playback_then_transcript_turn(speech_node):
    node, receive, started = speech_node
    handler = node._tts_handler = make_tts()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 16_000 * 12)

    def assert_blocked():
        assert node.brain._incoming_speech
        assert not any(e.kind == EventKind.USER for e in node.brain._events)
        node.brain._context._transport = lambda *_: [model_response(call_part("person_identity", {}))]
        run_turn(node.brain)
        assert started == []

    def synthesize(*_args, **_kwargs):
        assert_blocked()  # gated even before audio exists
        yield buffer.getvalue()

    waited = []

    def playback_wait(seconds):
        waited.append(seconds)
        node.brain._interaction_guard_started_at = time.monotonic() - 12
        assert_blocked()  # elapsed playback cannot time out the input gate

    handler._stream_tts_bytes = synthesize
    handler._publish_audio = lambda _: None
    handler._closing = SimpleNamespace(wait=playback_wait)
    handler.speak_text = lambda text, voice, on_start: handler._synthesize_to_topic(
        text, voice, time.perf_counter(), on_start
    )
    receive()
    assert_blocked()  # queued, worker not started
    handler._speech_queue.append(None)
    handler._speech_loop()
    assert waited == [12.0]
    assert not node.brain._incoming_speech
    assert any(e.kind == EventKind.USER for e in node.brain._events)
    run_turn(node.brain)  # only this fresh turn consumes the completed input
    assert len(started) == 1 and started[0][0] == IDENTITY["id"]


@pytest.mark.parametrize("failure", ["missing", "queue_full", "queue_exception", "retry_failure", "close"])
def test_speech_failure_paths_deliver_once_and_release_gate(speech_node, monkeypatch, failure):
    node, receive, _ = speech_node
    if failure != "missing":
        handler = node._tts_handler = make_tts()
        if failure == "queue_full":
            handler._speech_queue_maxlen = 0
        elif failure == "queue_exception":

            def fail(*args, **kwargs):
                raise RuntimeError("queue failed")

            handler.speak_text_async = fail
    receive()
    if failure in {"retry_failure", "close"}:
        item = handler._speech_queue[0]
        if failure == "close":
            handler.close()
        else:
            attempts = []
            handler.speak_text = lambda *args: attempts.append(args) or False
            handler._speech_queue.append(None)
            monkeypatch.setattr("brain_client.transport.tts.time.sleep", lambda _: None)
            handler._speech_loop()
            assert len(attempts) == 2
        item.on_done(False)  # duplicate completion cannot repeat the transcript
    assert not node.brain._incoming_speech
    assert len([e for e in node.brain._events if e.kind == EventKind.USER]) == 1
