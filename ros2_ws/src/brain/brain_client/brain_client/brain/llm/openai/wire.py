# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Neutral types -> the OpenAI Responses API shapes."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from brain_client.brain.prompt import PORTRAIT_CAPTION, self_portrait

if TYPE_CHECKING:
    from brain_client.brain.llm.types import ToolSpec

FRAME_REMOVED = {"type": "input_text", "text": "[older camera frame removed]"}
WRIST_FRAME_REMOVED = {"type": "input_text", "text": "[older wrist camera frame removed]"}


def function_tools(specs: list[ToolSpec]) -> list[dict]:
    """The ``tools`` field: Responses flattens a function tool, with no nesting."""
    return [_function(spec) for spec in specs]


def image_part(jpeg: bytes) -> dict:
    return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"}


def is_image(part: dict) -> bool:
    return part.get("type") == "input_image"


def user_content(text: str, images: list[bytes]) -> dict:
    content = [{"type": "input_text", "text": text}, *(image_part(jpeg) for jpeg in images)]
    return {"role": "user", "content": content}


def reference_turns() -> list[dict]:
    """A pinned exchange showing the model its own body — ``instructions`` is
    text-only, so the portrait rides at the front of the input instead."""
    jpeg = self_portrait()
    if jpeg is None:
        return []
    return [
        user_content(PORTRAIT_CAPTION, [jpeg]),
        {"role": "assistant", "content": "Understood — that is what my model of robot looks like."},
    ]


def tool_outcome(call_id: str, outcome: str) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": outcome}


def _function(spec: ToolSpec) -> dict:
    function: dict = {"type": "function", "name": spec.name, "description": spec.description}
    # `strict` is left off deliberately: strict mode requires every property to
    # be listed in `required`, and skill inputs are frequently optional.
    function["parameters"] = spec.parameters or {"type": "object", "properties": {}, "required": []}
    return function
