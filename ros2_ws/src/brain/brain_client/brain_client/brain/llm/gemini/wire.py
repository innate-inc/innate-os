# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Neutral types -> the native Gemini REST shapes."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from brain_client.brain.prompt import PORTRAIT_CAPTION, self_portrait

if TYPE_CHECKING:
    from brain_client.brain.llm.types import ToolSpec

FRAME_REMOVED = {"text": "[older camera frame removed]"}
WRIST_FRAME_REMOVED = {"text": "[older wrist camera frame removed]"}

# Gemini's schema dialect is JSON Schema with the type names uppercased.
_TYPES = {"object": "OBJECT", "string": "STRING", "integer": "INTEGER", "number": "NUMBER", "boolean": "BOOLEAN"}


def tools_block(specs: list[ToolSpec]) -> list[dict]:
    """The whole ``tools`` field: one functionDeclarations block."""
    if not specs:
        return []
    return [{"functionDeclarations": [_declaration(spec) for spec in specs]}]


def image_part(jpeg: bytes) -> dict:
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}


def is_image(part: dict) -> bool:
    return "inlineData" in part


def user_content(text: str, images: list[bytes]) -> dict:
    return {"role": "user", "parts": [{"text": text}, *(image_part(jpeg) for jpeg in images)]}


def reference_turns() -> list[dict]:
    """A pinned exchange showing the model its own body. Gemini's
    systemInstruction is text-only, so the portrait rides at the front of every
    request's contents instead."""
    jpeg = self_portrait()
    if jpeg is None:
        return []
    return [
        {"role": "user", "parts": [{"text": PORTRAIT_CAPTION}, image_part(jpeg)]},
        {"role": "model", "parts": [{"text": "Understood — that is what my model of robot looks like."}]},
    ]


def _declaration(spec: ToolSpec) -> dict:
    declaration: dict = {"name": spec.name, "description": spec.description}
    if spec.parameters is not None:
        declaration["parameters"] = _schema(spec.parameters)
    return declaration


def _schema(schema: dict) -> dict:
    converted = {key: value for key, value in schema.items() if key != "properties"}
    if "type" in converted:
        converted["type"] = _TYPES.get(str(converted["type"]), converted["type"])
    properties = schema.get("properties")
    if isinstance(properties, dict):
        converted["properties"] = {name: _schema(sub) for name, sub in properties.items()}
    return converted
