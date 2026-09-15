# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Golden request bodies and a hand-written SSE transcript for the OpenAI Responses wire."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain_client.llm.models import resolve
from brain_client.llm.types import (
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
from brain_client.llm.wires.openai_responses import ADAPTER

GOLDENS = Path(__file__).parent / "goldens"
MODEL = resolve("gpt-5.4-mini")

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"
SYSTEM = "SYS"
TOOLS = (
    Tool(
        "wave", "Wave the arm.", {"type": "object", "properties": {"times": {"type": "integer"}}, "required": ["times"]}
    ),
    Tool("wait", "Do nothing.", {"type": "object", "properties": {}}),
)
REASONING_ITEM = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [{"type": "summary_text", "text": "planning"}],
    "encrypted_content": "enc123",
}
MESSAGE_ITEM = {
    "type": "message",
    "id": "msg_1",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "On it."}],
}
CALL_ITEM = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "wave", "arguments": '{"times":2}'}
TURN = (
    Thought("planning", native=(Wire.OPENAI_RESPONSES, REASONING_ITEM)),
    Text("On it.", native=(Wire.OPENAI_RESPONSES, MESSAGE_ITEM)),
    ToolCall("call_1", "wave", {"times": 2}, native=(Wire.OPENAI_RESPONSES, CALL_ITEM)),
)
FOREIGN = (Wire.ANTHROPIC, {"type": "text", "text": "On it."})
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


def golden(name: str) -> dict:
    return json.loads((GOLDENS / f"{name}.json").read_text())


def event(**payload: object) -> str:
    return json.dumps(payload)


def completed(**response: object) -> str:
    usage = {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 40},
        "output_tokens": 20,
        "output_tokens_details": {"reasoning_tokens": 8},
    }
    return event(
        type="response.completed", response={"id": "resp_1", "status": "completed", "usage": usage, **response}
    )


STREAM = [
    event(type="response.created", response={"id": "resp_1", "status": "in_progress"}),
    event(type="response.output_item.added", output_index=0, item={"type": "reasoning", "id": "rs_1", "summary": []}),
    event(type="response.reasoning_summary_text.delta", item_id="rs_1", summary_index=0, delta="planning"),
    event(type="response.output_item.done", output_index=0, item=REASONING_ITEM),
    event(type="response.output_text.delta", item_id="msg_1", content_index=0, delta="On "),
    event(type="response.output_text.delta", item_id="msg_1", content_index=0, delta="it."),
    event(type="response.output_item.done", output_index=1, item=MESSAGE_ITEM),
    event(type="response.function_call_arguments.delta", item_id="fc_1", delta='{"times":'),
    event(type="response.function_call_arguments.delta", item_id="fc_1", delta="2}"),
    event(type="response.output_item.done", output_index=2, item=CALL_ITEM),
    completed(),
]


def test_chat_body_matches_golden():
    assert ADAPTER.body(CHAT, MODEL) == golden("openai_responses_chat")


def test_json_body_matches_golden():
    assert ADAPTER.body(JSON_OUT, MODEL) == golden("openai_responses_json")


def test_native_turn_is_replayed_verbatim():
    items = ADAPTER.body(CHAT, MODEL)["input"]
    assert items[1:4] == [REASONING_ITEM, MESSAGE_ITEM, CALL_ITEM]


def test_foreign_native_is_encoded_from_parts():
    parts = (Thought("planning", FOREIGN), Text("On it.", FOREIGN), ToolCall("call_1", "wave", {"times": 2}, FOREIGN))
    foreign = Message(Role.ASSISTANT, parts)
    items = ADAPTER.body(Request(system=SYSTEM, messages=(MESSAGES[0], foreign)), MODEL)["input"]
    assert items[1:] == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "On it."}]},
        {"type": "function_call", "call_id": "call_1", "name": "wave", "arguments": '{"times": 2}'},
    ]


def test_default_thinking_without_summaries_omits_reasoning():
    body = ADAPTER.body(Request(system="", messages=MESSAGES[:1]), MODEL)
    assert "reasoning" not in body and "instructions" not in body


def test_minimal_rung_clamps_up_and_temperature_is_dropped():
    body = ADAPTER.body(
        Request(system=SYSTEM, messages=MESSAGES[:1], thinking=Thinking.MINIMAL, temperature=0.5), MODEL
    )
    assert body["reasoning"] == {"effort": "low"}
    assert "temperature" not in body


def test_stream_yields_deltas_then_one_reply():
    events = list(ADAPTER.events(iter(STREAM)))
    assert events[:3] == [ThoughtDelta("planning"), TextDelta("On "), TextDelta("it.")]
    reply = events[3]
    assert isinstance(reply, Reply) and len(events) == 4
    assert reply.message.parts == TURN  # every item rides its part as native
    assert reply.message.role == Role.ASSISTANT
    assert reply.usage == Usage(prompt=100, cached=40, output=20, thinking=8)
    assert reply.finish == Finish.TOOL_CALLS


def test_incomplete_on_max_output_tokens_is_length():
    lines = [
        event(type="response.output_text.delta", delta="On "),
        event(type="response.output_item.done", item=MESSAGE_ITEM),
        event(
            type="response.incomplete",
            response={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        ),
    ]
    reply = list(ADAPTER.events(iter(lines)))[-1]
    assert isinstance(reply, Reply) and reply.finish == Finish.LENGTH


def test_refusal_item_is_a_refusal_finish():
    refusal = {"type": "message", "id": "msg_1", "role": "assistant", "content": [{"type": "refusal", "refusal": "no"}]}
    lines = [event(type="response.output_item.done", item=refusal), completed()]
    reply = list(ADAPTER.events(iter(lines)))[-1]
    assert isinstance(reply, Reply) and reply.finish == Finish.REFUSAL
    assert reply.message.parts == ()


def test_failed_event_raises():
    lines = [event(type="response.failed", response={"status": "failed", "error": {"message": "boom"}})]
    with pytest.raises(LlmError, match="boom"):
        list(ADAPTER.events(iter(lines)))


def test_error_event_raises():
    with pytest.raises(LlmError, match="rate limited"):
        list(ADAPTER.events(iter([event(type="error", code="rate_limit", message="rate limited")])))


def test_stream_without_completion_raises():
    with pytest.raises(LlmError, match="before response.completed"):
        list(ADAPTER.events(iter(STREAM[:-1])))
