# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Focused Responses wire-format and real brain-loop checks, without network or ROS."""

import base64
import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest
import test_local_brain
from test_local_brain import JPEG, NAV_SKILL, run_turn

from brain_client.agents.types import TurnIntervals
from brain_client.brain.openai_context import OpenAIContext
from brain_client.brain.tools import assign_tool_names, build_tools
from brain_client.core.state import RunningSkill

agent_factory = test_local_brain.agent_factory


def message_item(text):
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def call_item(call_id="call_wait", name="wait", arguments="{}"):
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


def completed(*output, status="completed"):
    return {
        "type": "response.completed",
        "response": {
            "status": status,
            "output": list(output),
            "usage": {"input_tokens": 120, "input_tokens_details": {"cached_tokens": 40}, "output_tokens": 12},
        },
    }


def context(transport, **kwargs):
    return OpenAIContext(
        transport,
        model="gpt-6-astra",
        thinking_level="low",
        max_history=kwargs.pop("max_history", 60),
        max_image_turns=1,
        **kwargs,
    )


def test_native_replay_preserves_call_ids_reasoning_images_and_tool_schema():
    requests = []
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "opaque-test-payload",
        "summary": [{"type": "summary_text", "text": "The user asked to navigate."}],
    }
    native_call = call_item("call_nav", "navigate_to_position", '{"x":1,"y":2}')

    def transport(model, body):
        assert model == "gpt-6-astra"
        requests.append(copy.deepcopy(body))
        return [completed(reasoning, native_call)]

    ctx = context(transport, reference=[{"role": "user", "parts": [{"text": "Pinned reference"}]}])
    tools = build_tools(assign_tool_names([NAV_SKILL]), None)
    first = ctx.user_message("First observation", [JPEG, b"wrist-one"])
    response = ctx.generate(first, tools, "SYSTEM", latest_only_images=[1])
    assert ctx._history == [] and ctx.last_usage == {}
    decision = ctx.absorb(first, response, latest_only_images=[1])
    assert decision.thoughts == "The user asked to navigate."
    assert [(call.id, call.name, call.args) for call in decision.calls] == [
        ("call_nav", "navigate_to_position", {"x": 1, "y": 2})
    ]
    ctx.add_tool_outcomes([(decision.calls[0], "started")])
    ctx.generate(ctx.user_message("Second observation", [JPEG, b"wrist-two"]), tools, "SYSTEM", latest_only_images=[1])

    body = requests[-1]
    assert body["model"] == "gpt-6-astra" and body["reasoning"] == {"effort": "low"}
    assert body["instructions"] == "SYSTEM" and body["store"] is False
    assert body["parallel_tool_calls"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["input"][0]["content"][0]["text"] == "Pinned reference"
    assert body["input"][2:4] == [reasoning, native_call]
    assert body["input"][4] == {
        "type": "function_call_output",
        "call_id": "call_nav",
        "output": json.dumps({"outcome": "started"}),
    }
    images = [
        part["image_url"] for item in body["input"] for part in item.get("content", []) if part["type"] == "input_image"
    ]
    assert images == [
        "data:image/jpeg;base64," + base64.b64encode(jpeg).decode() for jpeg in [JPEG, JPEG, b"wrist-two"]
    ]
    assert len(first["parts"]) == 3  # send-time wrist masking did not mutate committed history
    schema = body["tools"][0]["parameters"]
    assert schema["type"] == "object"
    assert schema["properties"]["x"]["type"] == "number"
    assert schema["properties"]["local_frame"]["type"] == "boolean"
    assert schema["properties"]["mode"]["enum"] == ["fast", "safe"]
    assert ctx.last_usage == {"prompt": 120, "cached": 40, "output": 12}


@pytest.mark.parametrize("prefix", ["", "I am ", "I am in the kitchen."])
def test_completed_message_fills_missing_deltas_without_repeating_speech(prefix):
    message = message_item("I am in the kitchen.")
    events = [{"type": "response.output_text.delta", "delta": prefix}] if prefix else []
    ctx = context(lambda model, body: [*events, completed(message)])
    user = ctx.user_message("What room are you in?", [])
    speech = []
    response = ctx.generate(user, [], "SYSTEM", on_speech=speech.append)
    assert ctx.absorb(user, response).speech == "I am in the kitchen."
    assert "".join(speech) == "I am in the kitchen."


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"type": "response.output_text.delta", "delta": "unfinished"}],
        [{"type": "response.failed"}],
        [{"type": "response.incomplete"}],
        [completed(call_item(), status="incomplete")],
        [completed({"type": "reasoning", "summary": [], "encrypted_content": "opaque"})],
        [completed(call_item(arguments="[]"))],
    ],
    ids=["empty", "truncated", "failed", "incomplete", "wrong-status", "reasoning-only", "invalid-call"],
)
def test_failed_response_keeps_user_event_uncommitted(agent_factory, monkeypatch, events):
    agent, _ = agent_factory()
    agent._context = ctx = context(lambda model, body: events)
    executed = []
    monkeypatch.setattr(agent, "_execute", lambda call: executed.append(call))

    async def skip_backoff(*args, **kwargs):
        pass

    monkeypatch.setattr(agent, "_pause", skip_backoff)
    agent.on_user_message("Please wait here")
    run_turn(agent)
    assert len(agent._events) == 1
    assert ctx._history == [] and ctx.last_usage == {}
    assert executed == [] and agent.error_streak == 1
    assert not agent._turn_in_flight


@pytest.mark.parametrize("max_history", [2, 6])
def test_pruning_never_sends_orphaned_native_tool_outputs(max_history):
    requests = []

    def transport(model, body):
        requests.append(copy.deepcopy(body))
        return [completed(call_item(f"call_{len(requests)}"))]

    ctx = context(transport, max_history=max_history)
    for index in range(5):
        user = ctx.user_message(f"Observation {index}", [JPEG])
        response = ctx.generate(user, [], "SYSTEM")
        decision = ctx.absorb(user, response)
        ctx.add_tool_outcomes([(decision.calls[0], "ok")])
    for request in requests:
        seen = set()
        for item in request["input"]:
            if item.get("type") == "function_call":
                seen.add(item["call_id"])
            elif item.get("type") == "function_call_output":
                assert item["call_id"] in seen


def test_stop_discards_late_native_tool_speech_and_usage(agent_factory, monkeypatch):
    agent, _ = agent_factory()
    thinking, release, finished = threading.Event(), threading.Event(), threading.Event()

    def transport(model, body):
        thinking.set()
        try:
            assert release.wait(3)
            yield {"type": "response.output_text.delta", "delta": "A late answer. "}
            yield completed(message_item("A late answer. "), call_item("late_call", "wave"))
        finally:
            finished.set()

    agent._context = ctx = context(transport)
    executed = []
    monkeypatch.setattr(agent, "_execute", lambda call: executed.append(call))
    agent.on_user_message("Wave")
    try:
        assert agent.start() and thinking.wait(3)
        assert agent.stop()
    finally:
        release.set()
    assert finished.wait(3)
    assert ctx._history == [] and ctx.last_usage == {}
    assert executed == [] and agent._chat.spoken == []
    assert not agent._runtime.running and not agent._turn_in_flight


def test_cancelled_sequence_preserves_native_call_outputs_and_requires_new_request(agent_factory):
    from test_local_brain import WAVE_SKILL

    from brain_client.skills.registry import SkillRegistry

    agent, state = agent_factory()
    state.registry = SkillRegistry.from_metadata([WAVE_SKILL])
    agent._roster.active_skill_ids = lambda: [WAVE_SKILL["id"]]
    agent._runner.has_active_goal = False
    started, requests = [], []
    agent._runner.start_task = lambda *args: started.append(args)
    output = [call_item("stop", "stop_current_skill"), call_item("stale", "wave")]

    def transport(model, body):
        requests.append(copy.deepcopy(body))
        return [completed(*output)]

    agent._context = context(transport)
    agent.on_user_message("Stop and wait")
    run_turn(agent)
    output = [call_item("recovery", "wave")]
    agent.add_event("Navigation is ready again")
    run_turn(agent)
    assert started == []
    assert [tool["name"] for tool in requests[-1]["tools"]] == ["wait"]
    agent.on_user_message("Thanks, what can you see?")
    output = [message_item("I can see the room.")]
    run_turn(agent)
    output = [call_item("after_conversation", "wave")]
    run_turn(agent)
    assert started == []
    agent.on_user_message("Now wave")
    output = [call_item("new", "start_new_request"), call_item("too_early", "wave")]
    run_turn(agent)
    assert started == []
    output = [call_item("fresh", "wave")]
    run_turn(agent)
    assert len(started) == 1
    output = [call_item("idle", "wait")]
    run_turn(agent)
    # Every committed native call, including rejected stale actions, is answered
    # exactly once with its original ID. The next Responses request is complete.
    for request in requests:
        calls = [i["call_id"] for i in request["input"] if i.get("type") == "function_call"]
        results = [i["call_id"] for i in request["input"] if i.get("type") == "function_call_output"]
        assert calls == results


def test_cadence_waits_after_completion_and_never_overlaps_requests(agent_factory, monkeypatch):
    agent, state = agent_factory()
    state.current_directive = SimpleNamespace(
        get_prompt=lambda: "Wait quietly", get_turn_intervals=lambda: TurnIntervals(idle=0.06, supervision=0.09)
    )
    first, third, release = threading.Event(), threading.Event(), threading.Event()
    starts, ends, pauses = [], [], []
    active = peak = 0
    original_pause = agent._pause

    async def pause(seconds, **kwargs):
        pauses.append(seconds)
        await original_pause(seconds, **kwargs)

    def transport(model, body):
        nonlocal active, peak
        starts.append(time.monotonic())
        active += 1
        peak = max(peak, active)
        number = len(starts)
        try:
            if number == 1:
                first.set()
                assert release.wait(3)
            elif number == 2:
                state.primitive_running = RunningSkill("search", "local/search")
            else:
                third.set()
            yield completed(call_item(f"call_{number}"))
        finally:
            active -= 1
            ends.append(time.monotonic())

    monkeypatch.setattr(agent, "_pause", pause)
    agent._context = context(transport)
    try:
        assert agent.start() and first.wait(3)
        assert not third.wait(0.12) and len(starts) == 1  # blocked inference cannot start another heartbeat
        release.set()
        assert third.wait(3)
        assert agent.stop()
    finally:
        release.set()
    assert peak == 1
    assert pauses[:2] == [0.06, 0.09]
    assert starts[1] - ends[0] >= 0.05
    assert starts[2] - ends[1] >= 0.08


def test_config_selects_native_provider_and_reports_effective_model(agent_factory, monkeypatch):
    from brain_client.brain import agent as module
    from brain_client.core.config import _PARAM_DEFAULTS, BrainConfig

    assert BrainConfig(**_PARAM_DEFAULTS).brain_provider == "gemini"
    config = BrainConfig(**{**_PARAM_DEFAULTS, "brain_provider": "openai"})
    requests, traces = [], []

    def transport(model, body):
        requests.append(body)
        return [completed(call_item())]

    monkeypatch.setattr(module, "pick_openai_transport", lambda proxy: (transport, "innate-proxy"))
    agent, _ = agent_factory(trace=lambda event: traces.append(json.loads(event)), **vars(config))
    assert isinstance(agent._context, OpenAIContext)
    run_turn(agent)
    assert requests[0]["model"] == "gpt-6-astra"
    assert requests[0]["reasoning"]["effort"] == "low"
    request_trace = next(t for t in traces if t["ev"] == "turn_request")
    assert request_trace["body"]["input"] and "contents" not in request_trace["body"]
    end = next(t for t in traces if t["ev"] == "turn_end")
    assert (end["provider"], end["model"], end["reasoning_effort"]) == ("openai", "gpt-6-astra", "low")
    assert end["tokens"] == {"prompt": 120, "cached": 40, "output": 12}


@pytest.mark.parametrize(
    "override",
    [
        {"brain_provider": "typo"},
        {"idle_turn_interval": float("nan")},
        {"supervision_turn_interval": 0.0},
        {"brain_provider": "openai", "openai_model": " "},
        {"brain_provider": "openai", "openai_reasoning_effort": "light"},
    ],
)
def test_invalid_provider_configuration_fails_explicitly(override):
    from brain_client.core.config import _PARAM_DEFAULTS, BrainConfig

    with pytest.raises(ValueError):
        BrainConfig(**{**_PARAM_DEFAULTS, **override})
