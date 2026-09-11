# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Neutral types -> the Chat Completions shapes."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from brain_client.brain.prompt import PORTRAIT_CAPTION, self_portrait

if TYPE_CHECKING:
    from brain_client.brain.llm.types import ToolSpec

FRAME_REMOVED = {"type": "text", "text": "[older camera frame removed]"}
WRIST_FRAME_REMOVED = {"type": "text", "text": "[older wrist camera frame removed]"}


def function_tools(specs: list[ToolSpec]) -> list[dict]:
    return [_function(spec) for spec in specs]


def image_part(jpeg: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"}}


def is_image(part: dict) -> bool:
    return part.get("type") == "image_url"


def user_content(text: str, images: list[bytes]) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}, *(image_part(jpeg) for jpeg in images)]}


def reference_turns() -> list[dict]:
    """A pinned exchange showing the model its own body — the system message
    is text-only, so the portrait rides at the front of the messages instead."""
    jpeg = self_portrait()
    if jpeg is None:
        return []
    return [
        user_content(PORTRAIT_CAPTION, [jpeg]),
        {"role": "assistant", "content": "Understood — that is what my model of robot looks like."},
    ]


def assistant_message(text: str, calls: list[dict]) -> dict:
    """The model's turn as it is stored and replayed: text plus its tool calls,
    each with the id the server minted (the tool result has to quote it)."""
    message: dict = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}
            for call in calls
        ]
    return message


def tool_outcome(call_id: str, outcome: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": outcome}


def _function(spec: ToolSpec) -> dict:
    # Many servers reject a function with no `parameters` at all, so a
    # no-argument tool declares an empty object schema.
    parameters = spec.parameters or {"type": "object", "properties": {}, "required": []}
    return {
        "type": "function",
        "function": {"name": spec.name, "description": spec.description, "parameters": parameters},
    }
