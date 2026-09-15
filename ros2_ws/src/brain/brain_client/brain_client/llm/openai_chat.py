# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The OpenAI Chat Completions wire — the lingua franca a LAN server speaks.

vLLM, Ollama and NIM all answer here, so this adapter sticks to the fields
every one of them accepts and never to OpenAI's newest spellings. There is no
signed reasoning to carry: an assistant turn replays as the plain message dict
the wire itself defines.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass

from brain_client.llm.types import (
    Audio,
    Capabilities,
    Event,
    Finish,
    Image,
    LlmError,
    Message,
    Part,
    Reply,
    Request,
    Role,
    Text,
    TextDelta,
    Thinking,
    ToolCall,
    ToolResult,
    Usage,
    Wire,
)

BASE_URL = "https://api.openai.com/v1"

_FINISH = {
    "stop": Finish.STOP,
    "tool_calls": Finish.TOOL_CALLS,
    "length": Finish.LENGTH,
    "content_filter": Finish.REFUSAL,
}


@dataclass(frozen=True)
class OpenAIChatAdapter:
    wire: Wire = Wire.OPENAI_CHAT
    path: str = "/chat/completions"
    caps: Capabilities = Capabilities(
        wire=Wire.OPENAI_CHAT,
        audio_input=True,
        thought_summaries=False,
        json_schema=True,
        pinned=False,
        thinking_rungs=frozenset({Thinking.LOW, Thinking.MEDIUM, Thinking.HIGH, Thinking.XHIGH}),
    )

    def body(self, request: Request, model: str) -> dict:
        messages = [{"role": "system", "content": request.system}] if request.system else []
        messages += [m for message in request.messages for m in self._messages(message)]
        body: dict = {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": messages,
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        effort = self.caps.clamp(request.thinking)
        # OpenAI's chat wire 400s on tools + reasoning_effort for gpt-*; a LAN server takes both.
        if effort != Thinking.DEFAULT and not (request.tools and model.startswith("gpt-")):
            body["reasoning_effort"] = effort.value
        if request.json_schema is not None:
            schema = dict(request.json_schema)
            name = schema.pop("title", "") or "output"
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            }
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        return body

    def events(self, lines: Iterator[str]) -> Iterator[Event]:
        text: list[str] = []
        calls: dict[int, dict] = {}
        usage = Usage()
        reason = ""
        chunks = 0
        for line in lines:
            payload = _parse(line)
            if payload.get("error"):
                raise LlmError.protocol(_detail(payload))
            chunks += 1
            if payload.get("usage"):
                usage = _usage(payload["usage"])
            choices = payload.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                text.append(content)
                yield TextDelta(content)
            for call in delta.get("tool_calls") or []:
                _accumulate(calls, call)
            reason = choices[0].get("finish_reason") or reason
        if not chunks:
            raise LlmError.protocol("stream ended without a completion chunk")
        yield _reply(text, calls, usage, reason)

    def _messages(self, message: Message) -> list[dict]:
        if message.role == Role.USER:
            return [{"role": "user", "content": _user_content(message)}]
        if message.role == Role.TOOL:
            return [
                {"role": "tool", "tool_call_id": part.call_id, "content": part.text}
                for part in message.parts
                if isinstance(part, ToolResult)
            ]
        if message.native is not None and message.native[0] == self.wire:
            return [message.native[1]]
        return [_assistant_message(message)]


ADAPTER = OpenAIChatAdapter()


def _user_content(message: Message) -> list[dict]:
    content: list[dict] = []
    for part in message.parts:
        if isinstance(part, Text):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, Image):
            content.append({"type": "image_url", "image_url": {"url": _data_url(part.jpeg)}})
        elif isinstance(part, Audio):
            content.append(
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(part.wav).decode(), "format": "wav"}}
            )
    return content


def _assistant_message(message: Message) -> dict:
    """A turn we did not produce: its text and its calls, never its thought prose."""
    assistant: dict = {"role": "assistant", "content": message.text() or None}
    calls = message.calls()
    if calls:
        assistant["tool_calls"] = [_wire_call(call.id, call.name, json.dumps(call.args)) for call in calls]
    return assistant


def _wire_call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _data_url(jpeg: bytes) -> str:
    return f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"


def _accumulate(calls: dict[int, dict], call: dict) -> None:
    """Fold one streamed fragment into its call; ``arguments`` arrives split across chunks."""
    slot = calls.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
    function = call.get("function") or {}
    if call.get("id"):
        slot["id"] = call["id"]
    if function.get("name"):
        slot["name"] = function["name"]
    slot["arguments"] += function.get("arguments") or ""


def _reply(text: list[str], calls: dict[int, dict], usage: Usage, reason: str) -> Reply:
    ordered = [calls[index] for index in sorted(calls)]
    parts: list[Part] = [Text("".join(text))] if text else []
    parts += [ToolCall(call["id"], call["name"], _args(call["arguments"])) for call in ordered]
    assistant: dict = {"role": "assistant", "content": "".join(text) or None}
    if ordered:
        assistant["tool_calls"] = [_wire_call(c["id"], c["name"], c["arguments"]) for c in ordered]
    message = Message(Role.ASSISTANT, tuple(parts), native=(Wire.OPENAI_CHAT, assistant))
    return Reply(message, usage, _finish(bool(ordered), reason))


def _finish(has_calls: bool, reason: str) -> Finish:
    finish = _FINISH.get(reason, Finish.STOP)
    # Some servers report "stop" on a turn that did call a tool; the parts are the truth.
    return Finish.TOOL_CALLS if has_calls and finish == Finish.STOP else finish


def _usage(usage: dict) -> Usage:
    return Usage(
        prompt=usage.get("prompt_tokens") or 0,
        cached=(usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
        output=usage.get("completion_tokens") or 0,
        thinking=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
    )


def _args(arguments: str) -> dict:
    if not arguments.strip():
        return {}
    try:
        return json.loads(arguments) or {}
    except json.JSONDecodeError as error:
        raise LlmError.protocol(f"tool call arguments are not JSON: {error}") from error


def _detail(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message") or "the completion stream reported an error"
    return str(error)


def _parse(line: str) -> dict:
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise LlmError.protocol(f"unparsable chunk payload: {error}") from error
