# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The OpenAI Responses wire: a flat list of items in, typed ``response.*`` events out.

Reasoning here is an item of its own, not prose — it arrives encrypted on
``response.output_item.done`` and is replayed verbatim as its Thought's
``native``, so the model keeps its chain of thought across a tool call.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass

from brain_client.llm.types import (
    Capabilities,
    Event,
    Finish,
    Image,
    Json,
    LlmError,
    Message,
    Model,
    Part,
    Reply,
    Request,
    Role,
    Text,
    TextDelta,
    Thinking,
    Thought,
    ThoughtDelta,
    ToolCall,
    ToolResult,
    Usage,
    Wire,
)

BASE_URL = "https://api.openai.com/v1"

_TERMINAL = ("response.completed", "response.incomplete")
_FAILED = ("response.failed", "error")


@dataclass(frozen=True)
class OpenAIResponsesAdapter:
    wire: Wire = Wire.OPENAI_RESPONSES
    path: str = "/responses"
    caps: Capabilities = Capabilities(
        wire=Wire.OPENAI_RESPONSES,
        audio_input=False,
        thought_summaries=True,
        json_schema=True,
        pinned=False,
        thinking_rungs=frozenset({Thinking.LOW, Thinking.MEDIUM, Thinking.HIGH, Thinking.XHIGH}),
    )

    def body(self, request: Request, model: Model) -> Json:
        # store=false is what makes encrypted reasoning replayable: a stored response keeps it server-side instead.
        body: Json = {
            "model": model.name,
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if request.system:
            body["instructions"] = request.system
        body["input"] = [item for message in request.messages for item in self._items(message)]
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "strict": False,
                }
                for tool in request.tools
            ]
        reasoning = self._reasoning(request, model)
        if reasoning:
            body["reasoning"] = reasoning
        if request.json_schema is not None:
            schema = dict(request.json_schema)
            name = schema.pop("title", "") or "output"
            body["text"] = {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}}
        if request.max_tokens is not None:
            body["max_output_tokens"] = request.max_tokens
        return body  # request.temperature is dropped here: the reasoning models on this wire reject it

    def events(self, lines: Iterator[str]) -> Iterator[Event]:
        parts: list[Part] = []
        refused = False
        for line in lines:
            payload = _parse(line)
            kind = payload.get("type", "")
            if kind == "response.output_text.delta":
                yield TextDelta(payload.get("delta", ""))
            elif kind == "response.reasoning_summary_text.delta":
                yield ThoughtDelta(payload.get("delta", ""))
            elif kind == "response.output_item.done":
                item = payload.get("item") or {}
                refused = refused or _has_refusal(item)
                part = _part(item)
                if part is not None:
                    parts.append(part)
            elif kind in _TERMINAL:
                yield _reply(payload.get("response") or {}, parts, refused)
                return
            elif kind in _FAILED:
                raise LlmError.protocol(_detail(payload))
        raise LlmError.protocol("stream ended before response.completed")

    def _reasoning(self, request: Request, model: Model) -> Json:
        reasoning: Json = {}
        effort = self.caps.clamp(request.thinking, model.thinking)
        if effort != Thinking.DEFAULT:
            reasoning["effort"] = effort.value
        if request.thought_summaries:
            reasoning["summary"] = "auto"
        return reasoning

    def _items(self, message: Message) -> list[Json]:
        if message.role == Role.USER:
            return [{"type": "message", "role": "user", "content": _user_content(message)}]
        if message.role == Role.TOOL:
            return [
                {"type": "function_call_output", "call_id": part.call_id, "output": part.text}
                for part in message.parts
                if isinstance(part, ToolResult)
            ]
        items = [_assistant_item(part) for part in message.parts]
        return [item for item in items if item is not None]


ADAPTER = OpenAIResponsesAdapter()


def _user_content(message: Message) -> list[Json]:
    content: list[Json] = []
    for part in message.parts:
        if isinstance(part, Text):
            content.append({"type": "input_text", "text": part.text})
        elif isinstance(part, Image):
            content.append({"type": "input_image", "image_url": _data_url(part.jpeg)})
    return content


def _assistant_item(part: Part) -> Json | None:
    """The part's own item when this wire produced it; else its text or call, never foreign thought prose."""
    if (
        isinstance(part, (Text, Thought, ToolCall))
        and part.native is not None
        and part.native[0] == Wire.OPENAI_RESPONSES
    ):
        return part.native[1]
    if isinstance(part, Text):
        return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": part.text}]}
    if isinstance(part, ToolCall):
        return {"type": "function_call", "call_id": part.id, "name": part.name, "arguments": json.dumps(part.args)}
    return None


def _data_url(jpeg: bytes) -> str:
    return f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"


def _part(item: Json) -> Part | None:
    """The item as a typed part carrying it as ``native``; a reasoning item with no summary is an empty Thought."""
    kind = item.get("type", "")
    native = (Wire.OPENAI_RESPONSES, item)
    if kind == "message":
        text = "".join(b.get("text", "") for b in item.get("content") or [] if b.get("type") == "output_text")
        return Text(text, native=native) if text else None
    if kind == "function_call":
        return ToolCall(item.get("call_id", ""), item.get("name", ""), _args(item.get("arguments", "")), native=native)
    if kind == "reasoning":
        return Thought("".join(b.get("text", "") for b in item.get("summary") or []), native=native)
    return None


def _has_refusal(item: Json) -> bool:
    return any(block.get("type") == "refusal" for block in item.get("content") or [])


def _reply(response: Json, parts: list[Part], refused: bool) -> Reply:
    reason = (response.get("incomplete_details") or {}).get("reason") or ""
    message = Message(Role.ASSISTANT, tuple(parts))
    finish = _finish(parts, reason, refused)
    return Reply(message, _usage(response.get("usage") or {}), finish, "refusal" if refused else reason)


def _finish(parts: list[Part], reason: str, refused: bool) -> Finish:
    if refused or reason == "content_filter":
        return Finish.REFUSAL
    if reason == "max_output_tokens":
        return Finish.LENGTH
    if any(isinstance(part, ToolCall) for part in parts):
        return Finish.TOOL_CALLS
    return Finish.STOP


def _usage(usage: Json) -> Usage:
    return Usage(
        prompt=usage.get("input_tokens") or 0,
        cached=(usage.get("input_tokens_details") or {}).get("cached_tokens") or 0,
        output=usage.get("output_tokens") or 0,
        thinking=(usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0,
    )


def _args(arguments: str) -> Json:
    if not arguments.strip():
        return {}
    try:
        return json.loads(arguments) or {}
    except json.JSONDecodeError as error:
        raise LlmError.protocol(f"tool call arguments are not JSON: {error}") from error


def _detail(payload: Json) -> str:
    error = payload.get("error") or (payload.get("response") or {}).get("error") or {}
    return error.get("message") or payload.get("message") or "the response stream reported an error"


def _parse(line: str) -> Json:
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise LlmError.protocol(f"unparsable event payload: {error}") from error
