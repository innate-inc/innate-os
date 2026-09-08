# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""A local VLM (llama-server on the Jetson) as a drop-in for Gemini.

The brain speaks Gemini's native wire format end to end — contents/parts,
functionCall/functionResponse, tool declarations — so rather than teach the
context a second dialect, this transport translates one request body into an
OpenAI-style ``/v1/chat/completions`` call and folds the streamed deltas back
into Gemini-shaped chunks. :class:`~brain_client.brain.context.GeminiContext`
cannot tell the difference; prompt, tools and history stay untouched.

A 2B model cannot hold Gemini's "text is speech, stay silent otherwise"
discipline: left to free text it narrates its monologue aloud every update
and leaks reasoning. So the model is held to tool calls only (a grammar the
server enforces) and speech becomes one more tool, ``say`` — translated back
into text parts here, streamed as its argument arrives, so the rest of the
brain still sees plain speech. History is rewritten the same way, so the
model always sees its own past replies in the format it must produce.

Sampling is the server's business (its launch flags carry the model's
recommended values) except temperature, which the request pins low: it is
what keeps idle turns idle. Measured on Qwen3.5-2B (2026-09, 8 idle turns
each): 0.2 gave 8/8 silent waits, the model's recommended 0.7 invented a
navigation call on 5/8 — on a real robot that is the difference between safe
and not. The rest of the request fixes what shapes a robot turn: no
thinking, a short reply budget, and the tools.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from brain_client.brain.transport import Transport

CHAT_PATH = "/v1/chat/completions"
# The history cap assumes Gemini's million-token window; the server holds an 8K
# context (scripts/local_llm_server.sh) and a request past it is refused whole.
LOCAL_HISTORY_MAX_ENTRIES = 60
_MAX_REPLY_TOKENS = 256  # speech is one short sentence plus a call; a runaway reply must not stall the loop
_TEMPERATURE = 0.2  # see the module docstring before raising this
_TURN_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)  # image prefill is seconds on a Nano

SAY = "say"
_SAY_TOOL = {
    "type": "function",
    "function": {
        "name": SAY,
        "description": "Speak to the user out loud. One short sentence.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
}
_FORMAT_NOTE = (
    "\n\nOutput format: you answer ONLY with tool calls. To speak, call say (one short sentence). "
    "When nothing new happened — no user message, no skill event — call wait alone. "
    "Never call a skill the user did not ask for."
)
_SAY_TEXT_START = re.compile(r'"text"\s*:\s*"')

_GEMINI_TO_JSON_SCHEMA_TYPE = {
    "OBJECT": "object",
    "STRING": "string",
    "INTEGER": "integer",
    "NUMBER": "number",
    "BOOLEAN": "boolean",
    "ARRAY": "array",
}


def local_transport(base_url: str, model: str) -> Transport:
    """Reach a llama-server (or any OpenAI-compatible endpoint) at ``base_url``."""
    client = httpx.Client(base_url=base_url.rstrip("/"), timeout=_TURN_TIMEOUT)

    def stream(_model: str, body: dict) -> Iterator[dict]:
        request = to_chat_request(body, model)
        try:
            with client.stream("POST", CHAT_PATH, json=request) as resp:
                if resp.status_code != 200:
                    resp.read()
                    raise RuntimeError(f"local llm: HTTP {resp.status_code}: {resp.text[:200]}")
                yield from gemini_chunks(resp.iter_lines())
        except httpx.HTTPError as error:
            raise RuntimeError(f"local llm at {base_url} unreachable: {error!r}") from error

    return stream


# ================= request: Gemini -> OpenAI =================
def to_chat_request(body: dict, model: str) -> dict:
    system = "".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts") or [])
    messages: list[dict] = [{"role": "system", "content": system + _FORMAT_NOTE}]
    for content in body.get("contents") or []:
        messages.extend(_messages_from(content))
    tools = [_tool_from(d) for block in body.get("tools") or [] for d in block.get("functionDeclarations") or []]
    return {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": _MAX_REPLY_TOKENS,
        "temperature": _TEMPERATURE,
        "chat_template_kwargs": {"enable_thinking": False},
        "tools": [*tools, _SAY_TOOL],
        "tool_choice": "required",
    }


def _messages_from(content: dict) -> list[dict]:
    parts = content.get("parts") or []
    if content.get("role") == "model":
        return [_assistant_message(parts)]
    responses = [p["functionResponse"] for p in parts if "functionResponse" in p]
    if responses:
        # One tool message per answered call — the OpenAI shape has no
        # multi-response user turn.
        return [
            {"role": "tool", "tool_call_id": r.get("id") or r.get("name", ""), "content": json.dumps(r.get("response"))}
            for r in responses
        ]
    return [{"role": "user", "content": [_content_part(p) for p in parts if "text" in p or "inlineData" in p]}]


def _assistant_message(parts: list[dict]) -> dict:
    """A stored model turn, with its speech re-expressed as the say call it came from."""
    speech = "".join(p["text"] for p in parts if p.get("text"))
    calls = [_call_json(SAY, {"text": speech}, "")] if speech else []
    calls += [
        _call_json(
            p["functionCall"].get("name", ""), p["functionCall"].get("args") or {}, p["functionCall"].get("id") or ""
        )
        for p in parts
        if "functionCall" in p
    ]
    message: dict = {"role": "assistant", "content": ""}
    if calls:
        message["tool_calls"] = calls
    return message


def _call_json(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _content_part(part: dict) -> dict:
    if "text" in part:
        return {"type": "text", "text": part["text"]}
    blob = part["inlineData"]
    return {"type": "image_url", "image_url": {"url": f"data:{blob['mimeType']};base64,{blob['data']}"}}


def _tool_from(declaration: dict) -> dict:
    function: dict = {"name": declaration["name"], "description": declaration.get("description", "")}
    function["parameters"] = _json_schema(declaration.get("parameters") or {"type": "OBJECT", "properties": {}})
    return {"type": "function", "function": function}


def _json_schema(schema: dict) -> dict:
    """Gemini's upper-case schema types -> JSON Schema, recursively."""
    out = dict(schema)
    if "type" in out:
        out["type"] = _GEMINI_TO_JSON_SCHEMA_TYPE.get(str(out["type"]), str(out["type"]).lower())
    if isinstance(out.get("properties"), dict):
        out["properties"] = {name: _json_schema(sub) for name, sub in out["properties"].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _json_schema(out["items"])
    return out


# ================= response: OpenAI stream -> Gemini chunks =================
def gemini_chunks(lines: Iterator[str]) -> Iterator[dict]:
    """Fold an OpenAI SSE stream into Gemini-shaped chunks.

    A ``say`` call's text is yielded as plain-text deltas while its JSON
    argument is still arriving (the agent speaks from them); the other calls
    are yielded whole once the choice finishes, with the usage on that chunk.
    """
    calls: dict[int, dict] = {}
    finish = None
    usage: dict = {}
    for line in lines:
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload.strip() == "[DONE]":
            break
        chunk = json.loads(payload)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                yield _text_chunk(delta["content"])
            for fragment in delta.get("tool_calls") or []:
                call = _merge_call(calls, fragment)
                if call["name"] == SAY:
                    spoken = _say_text(call["arguments"])
                    if len(spoken) > len(call["spoken"]):
                        yield _text_chunk(spoken[len(call["spoken"]) :])
                        call["spoken"] = spoken
            finish = choice.get("finish_reason") or finish
    parts: list[dict] = []
    for _, call in sorted(calls.items()):
        if call["name"] != SAY:
            parts.append(_call_part(call))
            continue
        rest = _say_text(call["arguments"])[len(call["spoken"]) :]
        if rest:
            parts.append({"text": rest})
    yield {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": _finish_reason(finish)}],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "cachedContentTokenCount": 0,
        },
    }


def _text_chunk(text: str) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}


def _merge_call(calls: dict[int, dict], fragment: dict) -> dict:
    index = int(fragment.get("index") or 0)
    call = calls.setdefault(index, {"id": "", "name": "", "arguments": "", "spoken": ""})
    if fragment.get("id"):
        call["id"] = fragment["id"]
    function = fragment.get("function") or {}
    if function.get("name"):
        call["name"] += function["name"]
    call["arguments"] += function.get("arguments") or ""
    return call


def _say_text(arguments: str) -> str:
    """The text of a say call from its JSON argument, complete or still streaming.

    An open string decodes as the prefix so far; a fragment cut inside an
    escape sequence fails to decode and simply lands with the next one.
    """
    match = _SAY_TEXT_START.search(arguments)
    if match is None:
        return ""
    body = arguments[match.end() :]
    end = _unescaped_quote(body)
    if end is not None:
        body = body[:end]
    try:
        return str(json.loads(f'"{body}"'))
    except json.JSONDecodeError:
        return ""


def _unescaped_quote(text: str) -> int | None:
    escaped = False
    for i, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return i
    return None


def _call_part(call: dict) -> dict:
    try:
        args = json.loads(call["arguments"]) if call["arguments"].strip() else {}
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    return {"functionCall": {"name": call["name"], "args": args, "id": call["id"]}}


def _finish_reason(finish: str | None) -> str:
    return {"length": "MAX_TOKENS", "tool_calls": "STOP", "stop": "STOP"}.get(finish or "", finish or "STOP")
