# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Golden request bodies and a hand-written SSE transcript for the OpenAI Chat Completions wire."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain_client.llm.models import resolve
from brain_client.llm.types import (
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
    Tool,
    ToolCall,
    ToolResult,
    Usage,
    Wire,
)
from brain_client.llm.wires.openai_chat import ADAPTER

GOLDENS = Path(__file__).parent / "goldens"
MODEL = resolve("openai-chat:gpt-5.4-mini")
LOCAL_MODEL = resolve("qwen3-8b", base_url="http://10.0.0.5:8000/v1")

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"
WAV = b"RIFF" + b"\x00" * 40
SYSTEM = "SYS"
TOOLS = (
    Tool(
        "wave", "Wave the arm.", {"type": "object", "properties": {"times": {"type": "integer"}}, "required": ["times"]}
    ),
    Tool("wait", "Do nothing.", {"type": "object", "properties": {}}),
)
WIRE_CALL = {"id": "call_1", "type": "function", "function": {"name": "wave", "arguments": '{"times": 2}'}}
ENCODED = {"role": "assistant", "content": "On it.", "tool_calls": [WIRE_CALL]}
FOREIGN = (Wire.OPENAI_RESPONSES, {"id": "rs_1"})
TURN = (Thought("planning", FOREIGN), Text("On it.", FOREIGN), ToolCall("call_1", "wave", {"times": 2}, FOREIGN))
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
AUDIO = Request(system="", messages=(Message(Role.USER, (Audio(WAV), Text("Transcribe."))),), temperature=0.2)


def golden(name: str) -> dict:
    return json.loads((GOLDENS / f"{name}.json").read_text())


def chunk(delta: dict, finish_reason: str | None = None) -> str:
    choice = {"index": 0, "delta": delta, "finish_reason": finish_reason}
    return json.dumps({"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": MODEL.name, "choices": [choice]})


USAGE_CHUNK = json.dumps(
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 8},
        },
    }
)
STREAM = [
    chunk({"role": "assistant", "content": ""}),
    chunk({"content": "On "}),
    chunk({"content": "it."}),
    chunk(
        {
            "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function", "function": {"name": "wave", "arguments": ""}}
            ]
        }
    ),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"times":'}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": "2}"}}]}),
    chunk({}, finish_reason="tool_calls"),
    USAGE_CHUNK,
]


def test_chat_body_matches_golden():
    assert ADAPTER.body(CHAT, MODEL) == golden("openai_chat_chat")


def test_json_body_matches_golden():
    assert ADAPTER.body(JSON_OUT, MODEL) == golden("openai_chat_json")


def test_audio_body_matches_golden():
    assert ADAPTER.body(AUDIO, MODEL) == golden("openai_chat_audio")


def test_assistant_turn_is_encoded_from_parts_whatever_their_native():
    # This wire carries no per-part state: any native, its own or foreign, is ignored.
    assert ADAPTER.body(CHAT, MODEL)["messages"][2] == ENCODED


def test_reasoning_effort_is_dropped_for_gpt_with_tools_and_kept_without():
    assert "reasoning_effort" not in ADAPTER.body(CHAT, MODEL)
    assert ADAPTER.body(CHAT, LOCAL_MODEL)["reasoning_effort"] == "low"
    assert "reasoning_effort" not in ADAPTER.body(Request(system="", messages=MESSAGES[:1]), LOCAL_MODEL)


def test_stream_yields_deltas_then_one_reply():
    events = list(ADAPTER.events(iter(STREAM)))
    assert events[:2] == [TextDelta("On "), TextDelta("it.")]
    reply = events[2]
    assert isinstance(reply, Reply) and len(events) == 3
    assert reply.message.role == Role.ASSISTANT
    assert reply.message.parts == (Text("On it."), ToolCall("call_1", "wave", {"times": 2}))
    assert reply.usage == Usage(prompt=100, cached=40, output=20, thinking=8)
    assert reply.finish == Finish.TOOL_CALLS


def test_length_finish():
    reply = list(ADAPTER.events(iter([chunk({"content": "On "}, finish_reason="length")])))[-1]
    assert isinstance(reply, Reply) and reply.finish == Finish.LENGTH


def test_content_filter_is_a_refusal_finish():
    reply = list(ADAPTER.events(iter([chunk({}, finish_reason="content_filter")])))[-1]
    assert isinstance(reply, Reply) and reply.finish == Finish.REFUSAL
    assert reply.message.parts == ()


def test_tool_call_without_tool_calls_finish_reason_still_finishes_tool_calls():
    lines = [*STREAM[3:6], chunk({}, finish_reason="stop")]
    reply = list(ADAPTER.events(iter(lines)))[-1]
    assert isinstance(reply, Reply) and reply.finish == Finish.TOOL_CALLS


def test_error_payload_raises():
    with pytest.raises(LlmError, match="context length"):
        list(ADAPTER.events(iter([json.dumps({"error": {"message": "context length exceeded"}})])))


def test_empty_stream_raises():
    with pytest.raises(LlmError, match="without a completion chunk"):
        list(ADAPTER.events(iter([])))
