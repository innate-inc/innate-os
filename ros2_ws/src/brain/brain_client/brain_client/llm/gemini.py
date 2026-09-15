# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini's own REST API: thought summaries, inline frames, and an explicit context cache.

The native wire — unlike the OpenAI-compatible layer — streams thought
summaries (parts flagged ``thought: true``) and takes a server-side cache
handle, which is what the robot's chat and its spatial memory were built on.
A turn the model produced here is replayed verbatim from ``Message.native``,
signatures and all; a turn assembled from parts carries the documented skip
sentinel instead, because Gemini validates the signature on a replayed call.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from brain_client.llm.provider import Provider
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
    Thought,
    ThoughtDelta,
    Tool,
    ToolCall,
    ToolResult,
    Usage,
    Wire,
)

BASE_URL = "https://generativelanguage.googleapis.com"
CACHED_CONTENTS_PATH = "/v1beta/cachedContents"

_PIN_TIMEOUT_S = 120.0  # a cache build carries MBs of frames, not one turn's prompt

# The dummy signature Google documents for a function call replayed without one
# (https://ai.google.dev/gemini-api/docs/thought-signatures#faqs) — a call from
# another wire, or one whose thought part was dropped, is rejected without it.
_SKIP_SIGNATURE = base64.b64encode(b"skip_thought_signature_validator").decode()

# A function declaration's `parameters` is an OpenAPI 3.0.3 subset: these keywords are
# rejected outright, or (additionalProperties) accepted and then silently mishandled.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "$schema",
        "$defs",
        "$ref",
        "const",
        "discriminator",
        "examples",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "prefixItems",
        "title",
    }
)
_TITLE = frozenset({"title"})

_SCHEMA_VALUED = frozenset({"items", "additionalProperties"})
_SCHEMA_LIST_VALUED = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SCHEMA_MAP_VALUED = frozenset({"properties", "$defs"})

_BLOCKED_FINISH = frozenset({"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"})


@dataclass(frozen=True)
class GeminiAdapter:
    wire: Wire = Wire.GEMINI
    path: str = "/v1beta/models/{model}:streamGenerateContent?alt=sse"
    caps: Capabilities = Capabilities(
        wire=Wire.GEMINI,
        audio_input=True,
        thought_summaries=True,
        json_schema=True,
        pinned=True,
        thinking_rungs=frozenset({Thinking.MINIMAL, Thinking.LOW, Thinking.MEDIUM, Thinking.HIGH}),
    )

    def body(self, request: Request, model: str) -> dict:
        body: dict = {}
        if request.pinned:
            body["cachedContent"] = request.pinned  # the cache holds the system prompt and the tools
        elif request.system:
            body["systemInstruction"] = _instruction(request.system)
        body["contents"] = [_content(message) for message in request.messages]
        if request.tools and not request.pinned:
            body["tools"] = [{"functionDeclarations": [_declaration(tool) for tool in request.tools]}]
        config = _generation_config(request, self.caps.clamp(request.thinking))
        if config:
            body["generationConfig"] = config
        return body

    def events(self, lines: Iterator[str]) -> Iterator[Event]:
        raw: list[dict] = []
        usage: dict = {}
        finish_reason = ""
        block_reason = ""
        for payload in lines:
            chunk = _chunk(payload)
            usage = chunk.get("usageMetadata") or usage
            candidate = (chunk.get("candidates") or [{}])[0]
            finish_reason = candidate.get("finishReason") or finish_reason
            block_reason = (chunk.get("promptFeedback") or {}).get("blockReason") or block_reason
            for part in (candidate.get("content") or {}).get("parts") or []:
                raw.append(part)
                text = part.get("text")
                if not text:
                    continue
                yield ThoughtDelta(text) if part.get("thought") else TextDelta(text)
        parts = _reply_parts(raw)
        # Thought prose is display-only and re-sending it is billed as input: the replay
        # keeps every other part exactly as it arrived, signatures included.
        kept = [part for part in raw if not part.get("thought")] or [{"text": ""}]
        message = Message(Role.ASSISTANT, tuple(parts), native=(Wire.GEMINI, {"role": "model", "parts": kept}))
        yield Reply(message, _usage(usage), _finish(parts, finish_reason, block_reason))


ADAPTER = GeminiAdapter()


@dataclass(frozen=True)
class GeminiProvider(Provider):
    """The one wire with words for an explicit cache: contents pinned server-side, then named per request."""

    def pin(self, system: str, messages: Sequence[Message], *, ttl_s: int, display_name: str = "") -> str:
        body: dict = {"model": f"models/{self.model}"}
        if system:
            body["systemInstruction"] = _instruction(system)
        body["contents"] = [_content(message) for message in messages]
        body["ttl"] = f"{ttl_s}s"
        if display_name:
            body["displayName"] = display_name
        name = self.http.post_json(CACHED_CONTENTS_PATH, body, timeout=_PIN_TIMEOUT_S).get("name")
        if not name:
            raise LlmError.protocol("cachedContents answered without a name")
        return str(name)

    def unpin(self, handle: str) -> None:
        self.http.delete(f"/v1beta/{handle}")


def _instruction(system: str) -> dict:
    return {"parts": [{"text": system}]}


def _content(message: Message) -> dict:
    if message.role == Role.ASSISTANT:
        return _model_turn(message)
    if message.role == Role.TOOL:
        parts = [_function_response(part) for part in message.parts if isinstance(part, ToolResult)]
        return {"role": "user", "parts": parts}
    user = [_user_part(part) for part in message.parts]
    return {"role": "user", "parts": [part for part in user if part is not None]}


def _model_turn(message: Message) -> dict:
    if message.native is not None and message.native[0] == Wire.GEMINI:
        return message.native[1]
    parts: list[dict] = []
    signed = False
    for part in message.parts:
        if isinstance(part, Text):
            parts.append({"text": part.text})
        elif isinstance(part, ToolCall):
            # Only the turn's first call is validated, so only it needs the sentinel.
            encoded: dict = {"functionCall": _function_call(part)}
            if not signed:
                encoded["thoughtSignature"] = _SKIP_SIGNATURE
                signed = True
            parts.append(encoded)
    return {"role": "model", "parts": parts or [{"text": ""}]}  # a turn with no parts is rejected


def _user_part(part: Part) -> dict | None:
    if isinstance(part, Text):
        return {"text": part.text}
    if isinstance(part, Image):
        return _inline(part.jpeg, "image/jpeg")
    if isinstance(part, Audio):
        return _inline(part.wav, "audio/wav")
    return None


def _inline(data: bytes, mime_type: str) -> dict:
    return {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(data).decode()}}


def _function_call(call: ToolCall) -> dict:
    encoded = {"name": call.name, "args": call.args}
    if call.id:
        encoded["id"] = call.id
    return encoded


def _function_response(result: ToolResult) -> dict:
    response: dict = {"name": result.name, "response": {"outcome": result.text}}
    if result.call_id:
        response["id"] = result.call_id
    return {"functionResponse": response}


def _declaration(tool: Tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": _pruned(tool.parameters, _UNSUPPORTED_SCHEMA_KEYS),
    }


def _generation_config(request: Request, thinking: Thinking) -> dict:
    config: dict = {}
    thinking_config: dict = {}
    if request.thought_summaries:
        thinking_config["includeThoughts"] = True
    if thinking != Thinking.DEFAULT:
        thinking_config["thinkingLevel"] = thinking.value
    if thinking_config:
        config["thinkingConfig"] = thinking_config
    if request.temperature is not None:
        config["temperature"] = request.temperature
    if request.max_tokens is not None:
        config["maxOutputTokens"] = request.max_tokens
    if request.json_schema is not None:
        config["responseMimeType"] = "application/json"
        config["responseJsonSchema"] = _pruned(request.json_schema, _TITLE)
    return config


def _pruned(schema: dict, dropped: frozenset[str]) -> dict:
    """``schema`` without the ``dropped`` keywords, through every nested schema position."""
    kept: dict = {}
    for key, value in schema.items():
        if key in dropped:
            continue
        if key in _SCHEMA_MAP_VALUED and isinstance(value, dict):
            kept[key] = {name: _pruned(sub, dropped) if isinstance(sub, dict) else sub for name, sub in value.items()}
        elif key in _SCHEMA_LIST_VALUED and isinstance(value, list):
            kept[key] = [_pruned(item, dropped) if isinstance(item, dict) else item for item in value]
        elif key in _SCHEMA_VALUED and isinstance(value, dict):
            kept[key] = _pruned(value, dropped)
        else:
            kept[key] = value
    return kept


def _chunk(payload: str) -> dict:
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError as error:
        raise LlmError.protocol(f"unparseable chunk: {error}") from error
    if not isinstance(chunk, dict):
        raise LlmError.protocol(f"chunk is not an object: {payload[:120]}")
    error_body = chunk.get("error")
    if error_body is not None:
        raise LlmError.protocol(str(error_body.get("message") or error_body))
    return chunk


def _reply_parts(raw: list[dict]) -> list[Part]:
    parts: list[Part] = []
    for part in raw:
        call = part.get("functionCall")
        if call is not None:
            parts.append(ToolCall(call.get("id") or "", call.get("name") or "", call.get("args") or {}))
            continue
        text = part.get("text")
        if not text:
            continue
        if part.get("thought"):
            parts.append(Thought(text))
            continue
        last = parts[-1] if parts else None
        if isinstance(last, Text):
            parts[-1] = Text(last.text + text)
            continue
        parts.append(Text(text))
    return parts


def _usage(usage: dict) -> Usage:
    return Usage(
        prompt=usage.get("promptTokenCount", 0),
        cached=usage.get("cachedContentTokenCount", 0),
        output=usage.get("candidatesTokenCount", 0),
        thinking=usage.get("thoughtsTokenCount", 0),
    )


def _finish(parts: list[Part], finish_reason: str, block_reason: str) -> Finish:
    if any(isinstance(part, ToolCall) for part in parts):
        return Finish.TOOL_CALLS
    if finish_reason == "MAX_TOKENS":
        return Finish.LENGTH
    if block_reason or finish_reason in _BLOCKED_FINISH:
        return Finish.REFUSAL
    return Finish.STOP
