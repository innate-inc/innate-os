# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The Anthropic Messages wire.

Thinking is adaptive on every current Claude model: ``budget_tokens`` and
``{"type": "disabled"}`` are 400s there, and the effort rung rides
``output_config`` instead. Signed thinking blocks come back as the ``native``
of their Thought part and are replayed verbatim — a thought from another wire
is dropped, never sent as a signature-less thinking block.
``request.temperature`` is dropped: current models reject sampling parameters.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

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
    Tool,
    ToolCall,
    ToolResult,
    Usage,
    Wire,
)

BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"  # routing sends it as the anthropic-version header, beside x-api-key

_MAX_TOKENS = 16000  # the wire requires a cap; a turn that needs more is a bug upstream
_BREAKPOINTS = 4  # the API takes at most 4 cache_control blocks per request
_EPHEMERAL = {"type": "ephemeral"}
# Model.budget_thinking (Haiku 4.5, Sonnet/Opus 4.5 and older): a token budget per rung.
_BUDGETS = {
    Thinking.MINIMAL: 1024,
    Thinking.LOW: 2048,
    Thinking.MEDIUM: 4096,
    Thinking.HIGH: 8192,
    Thinking.XHIGH: 16384,
}
_FINISH = {
    "tool_use": Finish.TOOL_CALLS,
    "max_tokens": Finish.LENGTH,
    "model_context_window_exceeded": Finish.LENGTH,
    "refusal": Finish.REFUSAL,
}


@dataclass(frozen=True)
class AnthropicAdapter:
    wire: Wire = Wire.ANTHROPIC
    path: str = "/v1/messages"
    caps: Capabilities = Capabilities(
        wire=Wire.ANTHROPIC,
        audio_input=False,
        thought_summaries=True,
        json_schema=True,
        pinned=False,
        thinking_rungs=frozenset({Thinking.LOW, Thinking.MEDIUM, Thinking.HIGH, Thinking.XHIGH}),
    )

    def body(self, request: Request, model: Model) -> Json:
        max_tokens = request.max_tokens or _MAX_TOKENS
        body: Json = {"model": model.name, "max_tokens": max_tokens, "stream": True}
        if request.system:
            body["system"] = [{"type": "text", "text": request.system}]
        pins = _breakpoints(request.messages)
        body["messages"] = [self._message(m, i in pins) for i, m in enumerate(request.messages)]
        if request.tools:
            body["tools"] = [_tool(tool) for tool in request.tools]
        rung = self.caps.clamp(request.thinking, model.thinking)
        if not model.budget_thinking:
            body["thinking"] = _thinking(request.thought_summaries)
        elif budget := _budget(rung, max_tokens):
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        output = _output_config(request, rung, effort=not model.budget_thinking)
        if output:
            body["output_config"] = output
        return body

    def events(self, lines: Iterator[str]) -> Iterator[Event]:
        stream = _Stream()
        for line in lines:
            event = _payload(line)
            if event.get("type") == "error":
                raise LlmError.protocol(_detail(event))
            delta = stream.consume(event)
            if delta is not None:
                yield delta
        yield stream.reply()

    def _message(self, message: Message, pin: bool) -> Json:
        encoded = self._encode(message)
        content = encoded["content"]
        if not pin or not content:
            return encoded
        return {**encoded, "content": [*content[:-1], {**content[-1], "cache_control": dict(_EPHEMERAL)}]}

    def _encode(self, message: Message) -> Json:
        if message.role == Role.TOOL:
            results = [_tool_result(p) for p in message.parts if isinstance(p, ToolResult)]
            return {"role": "user", "content": results}
        if message.role != Role.ASSISTANT:
            return {"role": "user", "content": [_user_block(p) for p in message.parts if isinstance(p, (Text, Image))]}
        blocks = [_assistant_block(p) for p in message.parts]
        return {"role": "assistant", "content": [block for block in blocks if block is not None]}


ADAPTER = AnthropicAdapter()


def _thinking(summaries: bool) -> Json:
    return {"type": "adaptive", "display": "summarized"} if summaries else {"type": "adaptive"}


def _budget(rung: Thinking, max_tokens: int) -> int:
    """A budget model's thinking allowance for the asked rung: 0 when none is asked or none fits."""
    if rung == Thinking.DEFAULT:
        return 0
    budget = min(_BUDGETS[rung], max_tokens - 1024)  # must stay under max_tokens
    return budget if budget >= 1024 else 0


def _output_config(request: Request, rung: Thinking, *, effort: bool) -> Json:
    config: Json = {}
    if effort and rung != Thinking.DEFAULT:
        config["effort"] = rung.value
    if request.json_schema is not None:
        config["format"] = {"type": "json_schema", "schema": _schema(request.json_schema)}
    return config


def _schema(schema: Json) -> Json:
    """The schema without its ``title`` — a name this wire has no field for."""
    return {key: value for key, value in schema.items() if key != "title"}


def _tool(tool: Tool) -> Json:
    """No ``strict``: it requires ``additionalProperties: false``, which skill schemas do not carry."""
    return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}


def _breakpoints(messages: tuple[Message, ...]) -> frozenset[int]:
    """The pinned turns, newest ``_BREAKPOINTS`` only — a fifth ``cache_control`` block is a 400."""
    pinned = [i for i, message in enumerate(messages) if message.pin]
    return frozenset(pinned[-_BREAKPOINTS:])


def _user_block(part: Text | Image) -> Json:
    if isinstance(part, Text):
        return {"type": "text", "text": part.text}
    source = {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(part.jpeg).decode()}
    return {"type": "image", "source": source}


def _assistant_block(part: Part) -> Json | None:
    if isinstance(part, (Text, Thought, ToolCall)) and part.native is not None and part.native[0] == Wire.ANTHROPIC:
        return dict(part.native[1])  # a copy: pinning must not mutate the caller's native
    if isinstance(part, Text):
        return {"type": "text", "text": part.text}
    if isinstance(part, ToolCall):
        return {"type": "tool_use", "id": part.id, "name": part.name, "input": part.args}
    return None


def _tool_result(part: ToolResult) -> Json:
    return {"type": "tool_result", "tool_use_id": part.call_id, "content": part.text}


@dataclass
class _Stream:
    """One assistant turn assembled from its content blocks, keyed by the index the API opened them at."""

    blocks: dict[int, Json] = field(default_factory=dict)
    fragments: dict[int, list[str]] = field(default_factory=dict)
    usage: Usage = Usage()
    stop_reason: str = ""
    started: bool = False

    def consume(self, event: Json) -> TextDelta | ThoughtDelta | None:
        kind = event.get("type", "")
        if kind == "message_start":
            self._open(event.get("message", {}).get("usage", {}))
            return None
        if kind == "content_block_start":
            self._start(event.get("index", 0), event.get("content_block", {}))
            return None
        if kind == "content_block_delta":
            return self._delta(event.get("index", 0), event.get("delta", {}))
        if kind == "content_block_stop":
            self._stop(event.get("index", 0))
            return None
        if kind == "message_delta":
            self._end(event)
        return None

    def reply(self) -> Reply:
        if not self.started:
            raise LlmError.protocol("stream ended before message_start")
        content = [block for _, block in sorted(self.blocks.items()) if _replayable(block)]
        parts = _parts(content)
        message = Message(Role.ASSISTANT, parts)
        finish = _finish(self.stop_reason, any(isinstance(p, ToolCall) for p in parts))
        return Reply(message, self.usage, finish, self.stop_reason)

    def _open(self, usage: Json) -> None:
        self.started = True
        cached = usage.get("cache_read_input_tokens", 0)
        # input_tokens is the uncached remainder here; every other wire's prompt count includes the cache.
        prompt = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + cached
        self.usage = replace(self.usage, prompt=prompt, cached=cached)

    def _start(self, index: int, block: Json) -> None:
        kind = block.get("type", "")
        if kind == "text":
            self.blocks[index] = {"type": "text", "text": block.get("text", "")}
        elif kind == "thinking":
            self.blocks[index] = {
                "type": "thinking",
                "thinking": block.get("thinking", ""),
                "signature": block.get("signature", ""),
            }
        elif kind == "redacted_thinking":
            self.blocks[index] = {"type": "redacted_thinking", "data": block.get("data", "")}
        elif kind == "tool_use":
            self.blocks[index] = {
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": {},
            }
            self.fragments[index] = []

    def _delta(self, index: int, delta: Json) -> TextDelta | ThoughtDelta | None:
        block = self.blocks.get(index)
        if block is None:
            return None
        kind = delta.get("type", "")
        if kind == "text_delta":
            text = delta.get("text", "")
            block["text"] += text
            return TextDelta(text)
        if kind == "thinking_delta":
            text = delta.get("thinking", "")
            block["thinking"] += text
            return ThoughtDelta(text)
        if kind == "signature_delta":
            block["signature"] += delta.get("signature", "")
        elif kind == "input_json_delta":
            self.fragments[index].append(delta.get("partial_json", ""))
        return None

    def _stop(self, index: int) -> None:
        block = self.blocks.get(index)
        if block is None or block["type"] != "tool_use":
            return
        block["input"] = _arguments(self.fragments.get(index, []))

    def _end(self, event: Json) -> None:
        self.stop_reason = event.get("delta", {}).get("stop_reason") or ""
        self.usage = replace(self.usage, output=event.get("usage", {}).get("output_tokens", 0))


def _payload(line: str) -> Json:
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise LlmError.protocol(f"unparsable event: {error}") from error


def _detail(event: Json) -> str:
    error = event.get("error", {})
    return error.get("message") or error.get("type") or "unspecified stream error"


def _arguments(fragments: list[str]) -> Json:
    payload = "".join(fragments).strip()
    if not payload:
        return {}
    try:
        args = json.loads(payload)
    except json.JSONDecodeError as error:
        raise LlmError.protocol(f"unparsable tool arguments: {error}") from error
    if not isinstance(args, dict):
        raise LlmError.protocol(f"tool arguments are not an object: {payload[:80]}")
    return args


def _replayable(block: Json) -> bool:
    """The API rejects an unsigned thinking block (a stream cut before its signature) and an empty text block."""
    if block["type"] == "thinking":
        return bool(block["signature"])
    if block["type"] == "text":
        return bool(block["text"])
    return True


def _parts(content: list[Json]) -> tuple[Part, ...]:
    """Typed parts, each carrying its block as ``native``; a redacted_thinking block is an empty Thought."""
    parts: list[Part] = []
    for block in content:
        kind = block["type"]
        native = (Wire.ANTHROPIC, block)
        if kind == "thinking":
            parts.append(Thought(block["thinking"], native=native))
        elif kind == "redacted_thinking":
            parts.append(Thought("", native=native))
        elif kind == "text":
            parts.append(Text(block["text"], native=native))
        elif kind == "tool_use":
            parts.append(ToolCall(block["id"], block["name"], block["input"], native=native))
    return tuple(parts)


def _finish(stop_reason: str, calls: bool) -> Finish:
    mapped = _FINISH.get(stop_reason)
    if mapped is not None:
        return mapped
    return Finish.TOOL_CALLS if calls else Finish.STOP
