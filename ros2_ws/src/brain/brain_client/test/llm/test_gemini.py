# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Golden request bodies and a hand-written SSE transcript for the native Gemini wire."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain_client.llm.gemini import ADAPTER, CACHED_CONTENTS_PATH, GeminiProvider
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
    ThoughtDelta,
    Tool,
    ToolCall,
    ToolResult,
    Usage,
    Wire,
)

GOLDENS = Path(__file__).parent / "goldens"
MODEL = "gemini-3.6-flash"
SKIP_SIGNATURE = "c2tpcF90aG91Z2h0X3NpZ25hdHVyZV92YWxpZGF0b3I="  # b64("skip_thought_signature_validator")

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"
WAV = b"RIFF" + b"\x00" * 40
SYSTEM = "SYS"
TOOLS = (
    Tool(
        "wave", "Wave the arm.", {"type": "object", "properties": {"times": {"type": "integer"}}, "required": ["times"]}
    ),
    Tool("wait", "Do nothing.", {"type": "object", "properties": {}}),
)
NATIVE = {
    "role": "model",
    "parts": [
        {"text": "On it.", "thoughtSignature": "c2lnMTIz"},
        {"functionCall": {"name": "wave", "args": {"times": 2}, "id": "call_1"}, "thoughtSignature": "c2lnNDU2"},
    ],
}
TURN = (Thought("planning"), Text("On it."), ToolCall("call_1", "wave", {"times": 2}))
MESSAGES = (
    Message(Role.USER, (Text("Look at this."), Image(JPEG)), pin=True),
    Message(Role.ASSISTANT, TURN, native=(Wire.GEMINI, NATIVE)),
    Message(Role.TOOL, (ToolResult("call_1", "wave", "started"),), pin=True),
    Message(Role.USER, (Text("Now what?"), Image(JPEG)), pin=True),
)
CHAT = Request(
    system=SYSTEM, messages=MESSAGES, tools=TOOLS, thinking=Thinking.LOW, thought_summaries=True, max_tokens=1024
)
VERDICT = {
    "title": "Verdict",
    "type": "object",
    "properties": {"found": {"type": "boolean"}, "frame": {"type": "integer"}},
    "required": ["found", "frame"],
    "additionalProperties": False,
}
JSON_OUT = Request(
    system=SYSTEM,
    messages=(Message(Role.USER, (Image(JPEG), Text("Frame 1"), Text("Which frame?"))),),
    thinking=Thinking.LOW,
    json_schema=VERDICT,
)
AUDIO = Request(
    system="",
    messages=(Message(Role.USER, (Audio(WAV), Text("Transcribe."))),),
    thinking=Thinking.MINIMAL,
    thought_summaries=True,
    temperature=0.0,
    max_tokens=1024,
)

# Gemini streams a functionCall whole (it never fragments the arguments), one
# candidate per chunk, with usageMetadata repeated as the counts firm up.
TRANSCRIPT = (
    {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "Wave twice.", "thought": True}]}, "index": 0}],
        "usageMetadata": {"promptTokenCount": 120, "cachedContentTokenCount": 3400},
    },
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "On ", "thoughtSignature": "c2lnQQ=="}]}}]},
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "it."}]}}]},
    {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"name": "wave", "args": {"times": 2}, "id": "call_9"},
                            "thoughtSignature": "c2lnQg==",
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "cachedContentTokenCount": 3400,
            "candidatesTokenCount": 57,
            "thoughtsTokenCount": 44,
        },
    },
)


def stream(*chunks: dict) -> list:
    return list(ADAPTER.events(iter([json.dumps(chunk) for chunk in chunks])))


def golden(name: str) -> dict:
    return json.loads((GOLDENS / f"gemini_{name}.json").read_text())


def test_chat_body_matches_golden() -> None:
    assert ADAPTER.body(CHAT, MODEL) == golden("chat")


def test_json_body_matches_golden() -> None:
    assert ADAPTER.body(JSON_OUT, MODEL) == golden("json")


def test_audio_body_matches_golden() -> None:
    assert ADAPTER.body(AUDIO, MODEL) == golden("audio")


def test_native_turn_is_replayed_verbatim() -> None:
    assert ADAPTER.body(CHAT, MODEL)["contents"][1] == NATIVE


def test_foreign_native_is_encoded_from_parts() -> None:
    foreign = Message(Role.ASSISTANT, TURN, native=(Wire.OPENAI_RESPONSES, {"output": [{"id": "rs_1"}]}))
    body = ADAPTER.body(Request(system="", messages=(MESSAGES[0], foreign)), MODEL)
    assert body["contents"][1] == {
        "role": "model",
        "parts": [
            {"text": "On it."},
            {
                "functionCall": {"name": "wave", "args": {"times": 2}, "id": "call_1"},
                "thoughtSignature": SKIP_SIGNATURE,
            },
        ],
    }
    assert "systemInstruction" not in body


def test_tool_schema_drops_the_keywords_gemini_rejects() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Pick",
        "type": "object",
        "properties": {"item": {"type": "string", "title": "Item", "const": "sock"}},
        "additionalProperties": False,
    }
    tools = (Tool("pick", "Pick it up.", schema),)
    body = ADAPTER.body(Request(system="", messages=(MESSAGES[0],), tools=tools), MODEL)
    assert body["tools"][0]["functionDeclarations"][0] == {
        "name": "pick",
        "description": "Pick it up.",
        "parameters": {"type": "object", "properties": {"item": {"type": "string"}}},
    }


def test_thinking_is_clamped_and_omitted_by_default() -> None:
    plain = ADAPTER.body(Request(system="", messages=(MESSAGES[0],)), MODEL)
    assert "generationConfig" not in plain
    top = ADAPTER.body(Request(system="", messages=(MESSAGES[0],), thinking=Thinking.XHIGH), MODEL)
    assert top["generationConfig"] == {"thinkingConfig": {"thinkingLevel": "high"}}


def test_pinned_request_sends_only_the_handle() -> None:
    body = ADAPTER.body(Request(system=SYSTEM, messages=MESSAGES, pinned="cachedContents/c1"), MODEL)
    assert body["cachedContent"] == "cachedContents/c1"
    assert "systemInstruction" not in body
    assert "tools" not in body


def test_pinned_request_refuses_tools_the_cache_never_held() -> None:
    with pytest.raises(LlmError, match="pinned"):
        ADAPTER.body(Request(system=SYSTEM, messages=MESSAGES, tools=TOOLS, pinned="cachedContents/c1"), MODEL)


def test_pin_posts_the_cached_contents_body() -> None:
    http = FakeHttp({"name": "cachedContents/c1"})
    provider = GeminiProvider(ADAPTER, http, MODEL)  # pyright: ignore[reportArgumentType] — a fake Http
    assert provider.pin(SYSTEM, MESSAGES[:2], ttl_s=600, display_name="mars-memory") == "cachedContents/c1"
    path, body, timeout = http.posts[0]
    assert (path, timeout) == (CACHED_CONTENTS_PATH, 120.0)
    assert body["model"] == f"models/{MODEL}"
    assert body["systemInstruction"] == {"parts": [{"text": SYSTEM}]}
    assert body["contents"] == ADAPTER.body(CHAT, MODEL)["contents"][:2]
    assert (body["ttl"], body["displayName"]) == ("600s", "mars-memory")
    provider.unpin("cachedContents/c1")
    assert http.deletes == ["/v1beta/cachedContents/c1"]


def test_pin_without_a_name_raises_protocol() -> None:
    provider = GeminiProvider(ADAPTER, FakeHttp({}), MODEL)  # pyright: ignore[reportArgumentType] — a fake Http
    with pytest.raises(LlmError, match="without a name"):
        provider.pin("", MESSAGES[:1], ttl_s=600)


def test_stream_yields_deltas_then_one_reply() -> None:
    events = stream(*TRANSCRIPT)
    assert events[:3] == [ThoughtDelta("Wave twice."), TextDelta("On "), TextDelta("it.")]
    reply = events[3]
    assert isinstance(reply, Reply)
    assert reply.message.parts == (Thought("Wave twice."), Text("On it."), ToolCall("call_9", "wave", {"times": 2}))
    assert reply.message.native == (
        Wire.GEMINI,
        {
            "role": "model",
            "parts": [
                {"text": "On ", "thoughtSignature": "c2lnQQ=="},
                {"text": "it."},
                {
                    "functionCall": {"name": "wave", "args": {"times": 2}, "id": "call_9"},
                    "thoughtSignature": "c2lnQg==",
                },
            ],
        },
    )
    assert reply.usage == Usage(prompt=120, cached=3400, output=57, thinking=44)
    assert reply.finish == Finish.TOOL_CALLS
    assert len(events) == 4


def test_max_tokens_finishes_as_length() -> None:
    stopped = {"candidates": [{"content": {"parts": [{"text": "On "}]}, "finishReason": "MAX_TOKENS"}]}
    reply = stream(stopped)[-1]
    assert isinstance(reply, Reply)
    assert reply.finish == Finish.LENGTH


def test_blocked_prompt_finishes_as_refusal() -> None:
    blocked = {"candidates": [{"finishReason": "SAFETY"}], "promptFeedback": {"blockReason": "SAFETY"}}
    reply = stream(blocked)[-1]
    assert isinstance(reply, Reply)
    assert reply.finish == Finish.REFUSAL
    assert reply.message.parts == ()


def test_empty_stream_still_replies() -> None:
    events = stream()
    assert len(events) == 1
    reply = events[0]
    assert isinstance(reply, Reply)
    assert reply.message.parts == ()
    assert reply.message.native == (Wire.GEMINI, {"role": "model", "parts": [{"text": ""}]})
    assert (reply.usage, reply.finish) == (Usage(), Finish.STOP)


def test_in_band_error_raises_protocol() -> None:
    failed = {"error": {"code": 429, "message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}}
    with pytest.raises(LlmError, match="Resource has been exhausted"):
        stream(TRANSCRIPT[0], failed)


def test_unparseable_chunk_raises_protocol() -> None:
    with pytest.raises(LlmError, match="unparseable chunk"):
        list(ADAPTER.events(iter(["{not json"])))


class FakeHttp:
    """Records what :class:`GeminiProvider` sends and answers with one canned body."""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.posts: list[tuple[str, dict, float | None]] = []
        self.deletes: list[str] = []

    def post_json(self, path: str, body: dict, *, timeout: float | None = None) -> dict:
        self.posts.append((path, body, timeout))
        return self.response

    def delete(self, path: str, *, timeout: float | None = None) -> dict:
        self.deletes.append(path)
        return {}
