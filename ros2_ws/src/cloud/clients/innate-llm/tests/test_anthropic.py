# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Golden request bodies and a hand-written SSE transcript for the Anthropic Messages wire."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from innate_llm.models import resolve
from innate_llm.types import (
    Audio,
    Finish,
    Image,
    LlmError,
    Message,
    Reply,
    Request,
    Role,
    Text,
    TextDelta,
    Thinking,
    Thought,
    ThoughtDelta,
    Tool,
    ToolCall,
    ToolResult,
    Usage,
    Wire,
)
from innate_llm.wires.anthropic import ADAPTER

GOLDENS = Path(__file__).parent / "goldens"
MODEL = resolve("claude-sonnet-5")

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"
WAV = b"RIFF" + b"\x00" * 40
SYSTEM = "SYS"
TOOLS = (
    Tool(
        "wave", "Wave the arm.", {"type": "object", "properties": {"times": {"type": "integer"}}, "required": ["times"]}
    ),
    Tool("wait", "Do nothing.", {"type": "object", "properties": {}}),
)
THINKING_BLOCK = {"type": "thinking", "thinking": "planning", "signature": "sig123"}
TEXT_BLOCK = {"type": "text", "text": "On it."}
CALL_BLOCK = {"type": "tool_use", "id": "call_1", "name": "wave", "input": {"times": 2}}
NATIVE = {"role": "assistant", "content": [THINKING_BLOCK, TEXT_BLOCK, CALL_BLOCK]}
TURN = (
    Thought("planning", native=(Wire.ANTHROPIC, THINKING_BLOCK)),
    Text("On it.", native=(Wire.ANTHROPIC, TEXT_BLOCK)),
    ToolCall("call_1", "wave", {"times": 2}, native=(Wire.ANTHROPIC, CALL_BLOCK)),
)
FOREIGN = (Wire.OPENAI_RESPONSES, {"id": "rs_1"})
MESSAGES = (
    Message(Role.USER, (Text("Look at this."), Image(JPEG)), pin=True),
    Message(Role.ASSISTANT, TURN),
    Message(Role.TOOL, (ToolResult("call_1", "wave", "started"),), pin=True),
    Message(Role.USER, (Text("Now what?"), Image(JPEG)), pin=True),
)
CHAT = Request(
    system=SYSTEM, messages=MESSAGES, tools=TOOLS, thinking=Thinking.LOW, thought_summaries=True, max_tokens=1024
)
JSON_OUT = Request(
    system=SYSTEM,
    messages=(Message(Role.USER, (Image(JPEG), Text("Frame 1"), Text("Which frame?"))),),
    thinking=Thinking.LOW,
    json_schema={
        "title": "Verdict",
        "type": "object",
        "properties": {"found": {"type": "boolean"}, "frame": {"type": "integer"}},
        "required": ["found", "frame"],
        "additionalProperties": False,
    },
)

TRANSCRIPT = (
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "role": "assistant",
            "model": MODEL.name,
            "content": [],
            "usage": {"input_tokens": 120, "cache_read_input_tokens": 3400, "cache_creation_input_tokens": 80},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Wave twice."}},
    {"type": "ping"},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sigABC"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "On "}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "it."}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_9", "name": "wave"}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"times"'}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": ": 2}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 57}},
    {"type": "message_stop"},
)


def stream(*events: dict) -> list:
    return list(ADAPTER.events(iter([json.dumps(event) for event in events])))


def golden(name: str) -> dict:
    return json.loads((GOLDENS / f"anthropic_{name}.json").read_text())


def test_chat_body_matches_golden() -> None:
    assert ADAPTER.body(CHAT, MODEL) == golden("chat")


def test_json_body_matches_golden() -> None:
    assert ADAPTER.body(JSON_OUT, MODEL) == golden("json")


def test_native_turn_is_replayed_verbatim() -> None:
    assert ADAPTER.body(CHAT, MODEL)["messages"][1] == NATIVE


def test_foreign_native_is_encoded_from_parts() -> None:
    parts = (Thought("planning", FOREIGN), Text("On it.", FOREIGN), ToolCall("call_1", "wave", {"times": 2}, FOREIGN))
    foreign = Message(Role.ASSISTANT, parts)
    body = ADAPTER.body(Request(system="", messages=(MESSAGES[0], foreign)), MODEL)
    assert body["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "On it."},
            {"type": "tool_use", "id": "call_1", "name": "wave", "input": {"times": 2}},
        ],
    }


def test_thought_summaries_and_effort_are_opt_in() -> None:
    body = ADAPTER.body(Request(system="", messages=(MESSAGES[0],)), MODEL)
    assert body["thinking"] == {"type": "adaptive"}
    assert "output_config" not in body
    assert "system" not in body
    assert body["max_tokens"] == 16000


def test_minimal_thinking_clamps_to_the_lowest_rung() -> None:
    request = Request(system="", messages=(MESSAGES[0],), thinking=Thinking.MINIMAL)
    assert ADAPTER.body(request, MODEL)["output_config"] == {"effort": "low"}


def test_a_budget_model_gets_budget_tokens_and_no_effort() -> None:
    # Haiku 4.5 (and Sonnet/Opus 4.5 and older) 400 on adaptive thinking and output_config.effort.
    body = ADAPTER.body(Request(system="", messages=(MESSAGES[0],), thinking=Thinking.LOW), resolve("claude-haiku-4-5"))
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert "output_config" not in body


def test_xhigh_clamps_to_high_where_the_model_stops_there() -> None:
    body = ADAPTER.body(
        Request(system="", messages=(MESSAGES[0],), thinking=Thinking.XHIGH), resolve("claude-sonnet-4-6")
    )
    assert body["output_config"] == {"effort": "high"}
    assert ADAPTER.body(CHAT, MODEL)["output_config"]["effort"] == "low"


def test_only_the_last_four_pins_become_breakpoints() -> None:
    turns = tuple(Message(Role.USER, (Text(f"{i}"),), pin=True) for i in range(6))
    content = [
        message["content"][-1] for message in ADAPTER.body(Request(system="", messages=turns), MODEL)["messages"]
    ]
    assert [block.get("cache_control") for block in content] == [None, None] + [{"type": "ephemeral"}] * 4


def test_audio_is_rejected_before_the_request_is_built() -> None:
    request = Request(system="", messages=(Message(Role.USER, (Audio(WAV),)),))
    with pytest.raises(LlmError, match="audio input"):
        ADAPTER.caps.check(request)


def test_stream_yields_deltas_then_one_reply() -> None:
    events = stream(*TRANSCRIPT)
    assert events[:3] == [ThoughtDelta("Wave twice."), TextDelta("On "), TextDelta("it.")]
    reply = events[3]
    assert isinstance(reply, Reply)
    assert reply.message.parts == (
        Thought(
            "Wave twice.",
            native=(Wire.ANTHROPIC, {"type": "thinking", "thinking": "Wave twice.", "signature": "sigABC"}),
        ),
        Text("On it.", native=(Wire.ANTHROPIC, {"type": "text", "text": "On it."})),
        ToolCall(
            "toolu_9",
            "wave",
            {"times": 2},
            native=(Wire.ANTHROPIC, {"type": "tool_use", "id": "toolu_9", "name": "wave", "input": {"times": 2}}),
        ),
    )
    assert reply.usage == Usage(prompt=3600, cached=3400, output=57)  # prompt counts the cache, like every wire
    assert reply.finish == Finish.TOOL_CALLS
    assert len(events) == 4


def test_unsigned_thinking_is_dropped_from_the_replayed_turn() -> None:
    cut = [event for event in TRANSCRIPT if event.get("delta", {}).get("type") != "signature_delta"]
    reply = stream(*cut)[-1]
    assert isinstance(reply, Reply)
    assert [type(part) for part in reply.message.parts] == [Text, ToolCall]
    replayed = ADAPTER.body(Request(system="", messages=(MESSAGES[0], reply.message)), MODEL)["messages"][1]
    assert [block["type"] for block in replayed["content"]] == ["text", "tool_use"]


def test_max_tokens_finishes_as_length() -> None:
    head = TRANSCRIPT[:10]
    stopped = {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 1024}}
    reply = stream(*head, stopped)[-1]
    assert isinstance(reply, Reply)
    assert reply.finish == Finish.LENGTH


def test_refusal_finishes_as_refusal() -> None:
    stopped = {"type": "message_delta", "delta": {"stop_reason": "refusal"}, "usage": {"output_tokens": 3}}
    reply = stream(TRANSCRIPT[0], stopped)[-1]
    assert isinstance(reply, Reply)
    assert reply.finish == Finish.REFUSAL
    assert reply.message.parts == ()


def test_in_band_error_raises_protocol() -> None:
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    with pytest.raises(LlmError, match="Overloaded"):
        stream(TRANSCRIPT[0], overloaded)


def test_stream_without_a_message_start_raises() -> None:
    with pytest.raises(LlmError, match="message_start"):
        stream({"type": "message_stop"})
