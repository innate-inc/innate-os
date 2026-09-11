# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini's native request and response shapes <-> the Chat Completions wire.

The brain speaks Gemini's native shapes internally — ``contents`` of ``parts``,
``functionCall`` / ``functionResponse``, thought parts — and this module lets
those same bodies reach any ``/v1/chat/completions`` server: a vLLM or Ollama
on a computer on the robot's network, NVIDIA's hosted NIM API. Requests are
translated on the way out; the streamed deltas are shaped back into Gemini
chunks on the way in, so the conversation, its pruning, and the monitor never
learn a second wire format.

What does not translate is dropped or refused where it is met: Gemini's
thinking config has no universal counterpart (a server-specific switch rides
``extra_body`` instead), ``thoughtSignature`` is Gemini-only, and the context
cache and Files API paths are answered as unsupported so their callers latch
those tiers off.

PURE module: dict in, dict out, no I/O.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Iterator

_GENERATE_PATH = re.compile(r"^/v1beta/models/(?P<model>[^:]+):generateContent$")

_FINISH_REASONS = {"stop": "STOP", "tool_calls": "STOP", "length": "MAX_TOKENS", "content_filter": "SAFETY"}
_SCHEMA_TYPES = {"OBJECT": "object", "STRING": "string", "INTEGER": "integer", "NUMBER": "number", "BOOLEAN": "boolean"}


def generate_model(path: str) -> str | None:
    """The model named by a native ``generateContent`` path; None for any other path."""
    match = _GENERATE_PATH.match(path)
    return match.group("model") if match else None


def encode_request(model: str, body: dict, *, stream: bool, extra_body: dict) -> dict:
    """A native Gemini request body as a Chat Completions request.

    ``extra_body`` is merged last, so an operator's server-specific fields
    (``chat_template_kwargs``, ``max_tokens``…) win over anything derived here.
    """
    if "cachedContent" in body:
        raise ValueError("context caches do not exist on an OpenAI-compatible server")
    messages: list[dict] = []
    system = " ".join(
        part["text"] for part in (body.get("systemInstruction") or {}).get("parts") or [] if "text" in part
    )
    if system:
        messages.append({"role": "system", "content": system})
    for content in body.get("contents") or []:
        messages.extend(_messages(content))
    request: dict = {"model": model, "messages": messages}
    if stream:
        request["stream"] = True
        request["stream_options"] = {"include_usage": True}
    declarations = [d for block in body.get("tools") or [] for d in block.get("functionDeclarations") or []]
    if declarations:
        request["tools"] = [_function_tool(d) for d in declarations]
    request.update(_generation_fields(body.get("generationConfig") or {}))
    request.update(extra_body)
    return request


def decode_stream(chunks: Iterable[dict]) -> Iterator[dict]:
    """Chat Completions deltas as Gemini stream chunks.

    Text and reasoning deltas pass through one by one (text is what the voice
    speaks as it lands). Tool calls arrive in fragments keyed by ``index`` and
    are emitted whole, with the usage and finish reason, once the stream ends.
    A stream that stops before ``finish_reason`` (a dropped connection) or
    ends on anything but ``stop``/``tool_calls`` raises rather than yielding a
    half reply: committing one would consume the turn's events and could
    dispatch a call whose arguments were cut mid-JSON.
    """
    calls: dict[int, dict] = {}
    finish_reason: str | None = None
    usage: dict = {}
    for chunk in chunks:
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            parts = _delta_parts(delta)
            if parts:
                yield _chunk(parts)
            for piece in delta.get("tool_calls") or []:
                _absorb_call_piece(calls, piece)
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
    if finish_reason is None:
        raise RuntimeError("openai-compat stream ended without a finish_reason")
    if finish_reason not in ("stop", "tool_calls"):
        raise RuntimeError(f"openai-compat response did not complete: finish_reason={finish_reason}")
    final = _chunk([_call_part(calls[index]) for index in sorted(calls)], finish_reason=finish_reason)
    final["usageMetadata"] = _usage(usage)
    yield final


def decode_response(data: dict) -> dict:
    """A non-streaming Chat Completions response as a Gemini ``generateContent`` response."""
    choices = data.get("choices") or []
    message = choices[0].get("message") or {} if choices else {}
    parts = _delta_parts(message)
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        parts.append(
            _call_part(
                {
                    "id": call.get("id") or _minted_id(),
                    "name": function.get("name") or "",
                    "arguments": function.get("arguments") or "",
                }
            )
        )
    response = _chunk(parts, finish_reason=str(choices[0].get("finish_reason") or "") if choices else None)
    response["usageMetadata"] = _usage(data.get("usage") or {})
    return response


# ---------- request ----------


def _messages(content: dict) -> list[dict]:
    parts = content.get("parts") or []
    if content.get("role") == "model":
        return [_assistant_message(parts)]
    responses = [p["functionResponse"] for p in parts if "functionResponse" in p]
    if responses:
        return [_tool_message(response) for response in responses]
    return [{"role": "user", "content": [_user_part(p) for p in parts]}]


def _user_part(part: dict) -> dict:
    if "text" in part:
        return {"type": "text", "text": part["text"]}
    if "inlineData" in part:
        mime, data = part["inlineData"]["mimeType"], part["inlineData"]["data"]
        if mime.startswith("audio/"):
            return {"type": "input_audio", "input_audio": {"data": data, "format": mime.split("/", 1)[1]}}
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
    raise ValueError(f"no Chat Completions form for a {'/'.join(sorted(part))} part")


def _assistant_message(parts: list[dict]) -> dict:
    text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
    message: dict = {"role": "assistant", "content": text}
    calls = [p["functionCall"] for p in parts if "functionCall" in p]
    if calls:
        message["tool_calls"] = [
            {
                "id": call.get("id") or _minted_id(),
                "type": "function",
                "function": {"name": call.get("name") or "", "arguments": json.dumps(call.get("args") or {})},
            }
            for call in calls
        ]
    return message


def _tool_message(response: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": response.get("id") or "",
        "content": json.dumps(response.get("response") or {}),
    }


def _function_tool(declaration: dict) -> dict:
    # Many servers reject a function with no `parameters` at all, so a
    # no-argument tool declares an empty object schema.
    parameters = declaration.get("parameters")
    return {
        "type": "function",
        "function": {
            "name": declaration["name"],
            "description": declaration.get("description") or "",
            "parameters": _json_schema(parameters)
            if parameters
            else {"type": "object", "properties": {}, "required": []},
        },
    }


def _json_schema(schema: dict) -> dict:
    """Gemini's schema dialect is JSON Schema with the type names uppercased."""
    converted = {key: value for key, value in schema.items() if key != "properties"}
    if "type" in converted:
        converted["type"] = _SCHEMA_TYPES.get(str(converted["type"]), converted["type"])
    properties = schema.get("properties")
    if isinstance(properties, dict):
        converted["properties"] = {name: _json_schema(sub) for name, sub in properties.items()}
    return converted


def _generation_fields(config: dict) -> dict:
    fields: dict = {}
    if "temperature" in config:
        fields["temperature"] = config["temperature"]
    if config.get("responseMimeType") == "application/json":
        schema = config.get("responseSchema")
        fields["response_format"] = (
            {"type": "json_schema", "json_schema": {"name": "response", "schema": _json_schema(schema)}}
            if schema
            else {"type": "json_object"}
        )
    return fields


# ---------- response ----------


def _delta_parts(delta: dict) -> list[dict]:
    parts: list[dict] = []
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if reasoning:
        parts.append({"text": str(reasoning), "thought": True})
    if delta.get("content"):
        parts.append({"text": str(delta["content"])})
    return parts


def _absorb_call_piece(calls: dict[int, dict], piece: dict) -> None:
    index = int(piece.get("index") or 0)
    call = calls.get(index)
    if call is None:
        call = calls[index] = {"id": str(piece.get("id") or _minted_id()), "name": "", "arguments": ""}
    function = piece.get("function") or {}
    if function.get("name"):
        call["name"] = str(function["name"])
    if function.get("arguments"):
        call["arguments"] += str(function["arguments"])


def _call_part(call: dict) -> dict:
    return {"functionCall": {"name": call["name"], "args": _arguments(call["arguments"]), "id": call["id"]}}


def _arguments(raw: str) -> dict:
    """Arguments arrive as a JSON-encoded string; anything unreadable is no arguments."""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _minted_id() -> str:
    # A server that omits call ids still needs one echoed back with the result.
    return f"call_{uuid.uuid4().hex[:12]}"


def _chunk(parts: list[dict], finish_reason: str | None = None) -> dict:
    candidate: dict = {"content": {"role": "model", "parts": parts}}
    if finish_reason:
        candidate["finishReason"] = _FINISH_REASONS.get(finish_reason, finish_reason.upper())
    return {"candidates": [candidate]}


def _usage(usage: dict) -> dict:
    return {
        "promptTokenCount": usage.get("prompt_tokens", 0),
        "cachedContentTokenCount": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        "candidatesTokenCount": usage.get("completion_tokens", 0),
    }
