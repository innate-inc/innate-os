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
            "usage": {
                "input_tokens": 120,
                "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 60},
                "output_tokens": 12,
            },
        },
    }


def context(transport, **kwargs):
    return OpenAIContext(
        transport,
        model=kwargs.pop("model", "gpt-6-astra"),
        thinking_level="low",
        max_history=kwargs.pop("max_history", 60),
        max_image_turns=kwargs.pop("max_image_turns", 1),
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
    assert body["input"][0]["role"] == "developer"
    assert body["input"][0]["content"][0]["text"] == "SYSTEM" and body["store"] is False
    assert body["parallel_tool_calls"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["input"][1]["content"][0]["text"] == "Pinned reference"
    assert body["input"][3:5] == [reasoning, native_call]
    assert body["input"][5] == {
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
    assert ctx.last_usage == {"prompt": 120, "cached": 40, "cache_write": 60, "output": 12}


def cache_prefixes(body):
    """Rendered input prefixes selected for reuse, ignoring cache metadata."""
    items, prefixes = [], []
    for item in copy.deepcopy(body["input"]):
        parts = item.pop("content", None)
        items.append(item)
        if parts is not None:
            item["content"] = []
            for part in parts:
                marker = part.pop("prompt_cache_breakpoint", None)
                item["content"].append(part)
                if marker:
                    assert marker == {"mode": "explicit"}
                    prefixes.append(copy.deepcopy(items))
    return prefixes


def test_cache_boundary_survives_wrist_replacement_and_fresh_scratchpad():
    requests = []

    def transport(model, body):
        requests.append(copy.deepcopy(body))
        return [completed(call_item(f"call_{len(requests)}"))]

    ctx = context(transport, max_image_turns=10, service_tier="priority")
    tools = build_tools(assign_tool_names([NAV_SKILL]), None)
    for turn in range(4):
        user = ctx.user_message(f"Observation {turn}", [JPEG, f"wrist-{turn}".encode()])
        notes = ctx.user_message(f"Map notes revision {turn}", [f"map-{turn}".encode()])
        before = copy.deepcopy(ctx._history)
        response = ctx.generate(user, tools, "SYSTEM", latest_only_images=[1], live_context=notes)
        assert ctx._history == before  # neither cache marking nor wrist masking commits the request
        request = requests[-1]
        prefixes = cache_prefixes(request)
        assert request["prompt_cache_options"] == {"mode": "explicit"}
        assert request["service_tier"] == "priority"
        assert 1 <= len(prefixes) <= 3
        if turn >= 2:
            # The prior write is still an explicitly selected prefix after another
            # exchange, including its tool call/result, has entered the history.
            assert cache_prefixes(requests[-2])[-1] in prefixes
        for prefix in prefixes:
            assert "Map notes revision" not in json.dumps(prefix)
            assert f"Observation {turn}" not in json.dumps(prefix)
        assert request["input"][-2]["content"][0]["text"] == f"Map notes revision {turn}"
        images = [
            p["image_url"] for item in request["input"] for p in item.get("content", []) if p["type"] == "input_image"
        ]
        for old in range(turn):
            assert "data:image/jpeg;base64," + base64.b64encode(f"wrist-{old}".encode()).decode() not in images
            assert "data:image/jpeg;base64," + base64.b64encode(f"map-{old}".encode()).decode() not in images
        decision = ctx.absorb(user, response, latest_only_images=[1])
        ctx.add_tool_outcomes([(decision.calls[0], "ok")])
    assert len({r["prompt_cache_key"] for r in requests}) == 1
    ctx.clear()
    ctx.generate(ctx.user_message("New conversation", []), [], "SYSTEM")
    assert len(cache_prefixes(requests[-1])) == 1
    assert "Observation" not in json.dumps(requests[-1])
    assert ctx.last_usage == {}


@pytest.mark.parametrize("max_history,max_images", [(6, 10), (60, 1)])
def test_cache_recovers_after_batched_history_or_image_pruning(max_history, max_images):
    requests = []

    def transport(model, body):
        requests.append(copy.deepcopy(body))
        return [completed(call_item(f"call_{len(requests)}"))]

    ctx = context(transport, max_history=max_history, max_image_turns=max_images)
    recovered = False
    misses = 0
    for turn in range(8):
        user = ctx.user_message(f"Observation {turn}", [JPEG])
        response = ctx.generate(user, [], "SYSTEM")
        if turn >= 2:
            shared = cache_prefixes(requests[-2])[-1] in cache_prefixes(requests[-1])
            if not shared:
                misses += 1
            recovered |= bool(misses and shared)
        decision = ctx.absorb(user, response)
        ctx.add_tool_outcomes([(decision.calls[0], "ok")])
    assert misses and recovered


def test_older_openai_models_keep_implicit_caching():
    requests = []

    def transport(model, body):
        requests.append(copy.deepcopy(body))
        return [completed(message_item("OK"))]

    ctx = context(transport, model="gpt-5.4")
    user = ctx.user_message("Observation", [JPEG])
    ctx.absorb(user, ctx.generate(user, [], "SYSTEM"))
    ctx.generate(ctx.user_message("Next observation", []), [], "SYSTEM")
    for body in requests:
        assert body["instructions"] == "SYSTEM"
        assert "prompt_cache" not in json.dumps(body)


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


@pytest.mark.parametrize("service_tier", ["auto", "default", "priority"])
def test_config_selects_native_provider_and_reports_effective_model(agent_factory, monkeypatch, service_tier):
    from brain_client.brain import agent as module
    from brain_client.core.config import _PARAM_DEFAULTS, BrainConfig

    assert BrainConfig(**_PARAM_DEFAULTS).brain_provider == "gemini"
    config = BrainConfig(**{**_PARAM_DEFAULTS, "brain_provider": "openai", "openai_service_tier": service_tier})
    requests, traces = [], []

    def transport(model, body):
        requests.append(body)
        return [completed(call_item())]

    monkeypatch.setattr(module, "pick_openai_transport", lambda proxy: (transport, "innate-proxy"))
    agent, _ = agent_factory(trace=lambda event: traces.append(json.loads(event)), **vars(config))
    assert isinstance(agent._context, OpenAIContext)
    run_turn(agent)
    assert requests[0]["service_tier"] == service_tier
    assert requests[0]["model"] == "gpt-6-astra"
    assert requests[0]["reasoning"]["effort"] == "low"
    request_trace = next(t for t in traces if t["ev"] == "turn_request")
    assert request_trace["body"]["input"] and "contents" not in request_trace["body"]
    end = next(t for t in traces if t["ev"] == "turn_end")
    assert (end["provider"], end["model"], end["reasoning_effort"]) == ("openai", "gpt-6-astra", "low")
    assert end["tokens"] == {"prompt": 120, "cached": 40, "cache_write": 60, "output": 12}


@pytest.mark.parametrize(
    "override",
    [
        {"brain_provider": "typo"},
        {"openai_service_tier": "typo"},
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
