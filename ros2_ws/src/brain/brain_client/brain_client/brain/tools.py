# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill metadata -> Chat Completions function tools."""

from __future__ import annotations

import re

from brain_client.skills.registry import SkillMeta

STOP_SKILL = "stop_current_skill"
WAIT = "wait"
GO_TO_POINT_IN_VIEW = "go_to_point_in_view"

# Many servers reject a function whose parameters are missing, so even a
# no-argument tool declares an empty object schema.
_NO_PARAMETERS = {"type": "object", "properties": {}, "required": []}

# An explicit no-op keeps idle turns clean: without it, models tend to emit
# placeholder text ("[]", "Empty response") rather than returning nothing.
_WAIT_TOOL = {
    "type": "function",
    "function": {
        "name": WAIT,
        "description": "Do nothing until the next update. Use when there is nothing new to do or say.",
        "parameters": _NO_PARAMETERS,
    },
}

# Visual navigation grounding: the model points at a floor pixel and the robot
# projects it into a local navigation goal (brain/grounding.py). Declared only
# when navigate_to_position is among the active skills — it is the actuator.
_GO_TO_POINT_IN_VIEW_TOOL = {
    "type": "function",
    "function": {
        "name": GO_TO_POINT_IN_VIEW,
        "description": (
            "Drive toward a point you can see in the CURRENT camera frame. Give normalized image "
            "coordinates (0-1000) of a point ON THE FLOOR: y from the top, x from the left. For an "
            "object, point at the floor at its base. The robot drives to about 0.35 m short of that "
            "spot and turns to face it. Prefer this over navigate_to_position for anything you can "
            "see. Far targets are approached in capped steps — call it again after arriving."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "y": {"type": "integer", "description": "0-1000 from image top"},
                "x": {"type": "integer", "description": "0-1000 from image left"},
            },
            "required": ["y", "x"],
        },
    },
}

# Skill input "type" strings (python annotation names from skill introspection)
# -> JSON Schema types. Anything else is passed as a string with the expected
# type noted in the description.
_SCHEMA_TYPES = {"float": "number", "int": "integer", "str": "string", "bool": "boolean"}


def tool_name(skill_name: str) -> str:
    """Skill name -> valid function name.

    A function name may only hold ``[a-zA-Z0-9_-]`` (no dots) and must start
    with a letter or underscore — a digit-leading skill ("3d_scan") would 400
    every request while active.
    """
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", skill_name) or "skill"
    if not re.match(r"[a-zA-Z_]", name):
        name = "_" + name
    return name[:64]


def assign_tool_names(skills: list[SkillMeta]) -> list[tuple[str, SkillMeta]]:
    """Give every skill a unique function name, in roster order.

    Sanitizing/truncating can make two skill names collide (and a skill can
    shadow a built-in tool); colliding names get a numeric suffix so a call
    never silently dispatches to the wrong skill.
    """
    taken = {STOP_SKILL, WAIT, GO_TO_POINT_IN_VIEW}
    named: list[tuple[str, SkillMeta]] = []
    for meta in skills:
        base = name = tool_name(meta["name"])
        counter = 2
        while name in taken:
            suffix = f"_{counter}"
            name = base[: 64 - len(suffix)] + suffix
            counter += 1
        taken.add(name)
        named.append((name, meta))
    return named


def build_tools(
    named_skills: list[tuple[str, SkillMeta]],
    running_skill_name: str | None,
    *,
    can_go_to_point_in_view: bool = False,
    user_spoke: bool = False,
) -> list[dict]:
    """One function tool per available skill.

    ``named_skills`` is :func:`assign_tool_names`'s output — the caller derives
    its dispatch map from the same pairs, so the declared names and the
    dispatched names can never diverge.

    While a skill runs, the robot's only action is stopping it, so the
    declarations collapse. The shape depends on whether the user just spoke:
    offered any no-op tool, the model calls it and goes silent, so a turn
    carrying a user message gets stop_current_skill ALONE — plain text becomes
    the reply channel, and the description steers stop away from questions.
    """
    if running_skill_name is not None:
        stop = _tool(
            STOP_SKILL,
            f"Abort the currently running skill ({running_skill_name}). "
            + (
                "Only when the user asks you to stop or switch task, or the skill is clearly "
                "failing. Questions and conversation are NOT reasons to stop — answer those "
                "in text and let the skill continue."
                if user_spoke
                else "Use when it is clearly failing, no longer makes sense, or the user asks for something else."
            ),
        )
        return [stop] if user_spoke else [stop, _WAIT_TOOL]
    tools = [_skill_tool(name, meta) for name, meta in named_skills]
    if can_go_to_point_in_view:
        tools.append(_GO_TO_POINT_IN_VIEW_TOOL)
    tools.append(_WAIT_TOOL)
    return tools


def _tool(name: str, description: str, parameters: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters or _NO_PARAMETERS},
    }


def _skill_tool(name: str, meta: SkillMeta) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []
    for param_name, spec in (meta.get("inputs") or {}).items():
        properties[param_name] = _param_schema(spec if isinstance(spec, dict) else {})
        if isinstance(spec, dict) and spec.get("required"):
            required.append(param_name)
    parameters = {"type": "object", "properties": properties, "required": required}
    return _tool(name, meta.get("guidelines") or meta["name"], parameters)


def _param_schema(spec: dict) -> dict:
    declared_type = str(spec.get("type", "any"))
    schema: dict = {"type": _SCHEMA_TYPES.get(declared_type)}
    notes = []
    if schema["type"] is None:
        notes.append(f"type: {declared_type}")
        schema["type"] = "string"
    if "default" in spec:
        notes.append(f"default: {spec['default']}")

    enum_values = spec.get("enum")
    if enum_values and all(isinstance(v, str) for v in enum_values):
        schema["type"] = "string"
        schema["enum"] = list(enum_values)
    elif enum_values:
        notes.append(f"one of: {enum_values}")

    if notes:
        schema["description"] = ", ".join(notes)
    return schema
