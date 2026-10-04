# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure helpers for the expression driver — no ROS, no I/O."""

from __future__ import annotations

import json

Parts = frozenset[str]
ALL_PARTS: Parts = frozenset({"arm", "base", "head"})
NO_PARTS: Parts = frozenset()
# Skills that leave the body to this layer: express and head_emotion drive it through the driver,
# the rest never move it.
LEAVES_BODY = frozenset({"express", "head_emotion", "search_memory", "change_volume"})
# Skills that move the body without declaring it: they drive the base through their own ROS
# clients, and Nav2 tilts the head while navigating.
MASKS_ALL = frozenset({"navigate_to_position", "navigate_with_vision"})


def leaves_body(skill_id: str) -> bool:
    return _name(skill_id) in LEAVES_BODY


def parts_of(skill_id: str, declared: object) -> Parts:
    """The parts a running skill takes from the expression layer.

    ``declared`` is the run's ``body`` list from the skills server, the parts its interface
    declarations reach; a skill that declares none — or is missing the field (an older server, the
    app's mirror of a run) — may still move anything, so it masks everything.
    """
    name = _name(skill_id)
    if name in LEAVES_BODY:
        return NO_PARTS
    if name in MASKS_ALL or not isinstance(declared, list):
        return ALL_PARTS
    parts = frozenset(str(part) for part in declared) & ALL_PARTS
    return parts or ALL_PARTS


def _name(skill_id: str) -> str:
    return skill_id.rsplit("/", 1)[-1]


def is_mad_mode(robot_info: str) -> bool:
    """Whether a /robot/info payload says the drive is in Mad mode, where mars_app braces the arm.

    Mad is the preset whose id is "mad" in the payload's own table, as motor_sound reads it; a
    malformed payload reads as not Mad.
    """
    try:
        info = json.loads(robot_info)
    except json.JSONDecodeError:
        return False
    if not isinstance(info, dict):
        return False
    scale, modes = info.get("drive_speed_scale"), info.get("drive_speed_modes")
    if not isinstance(scale, (int, float)) or not isinstance(modes, list):
        return False
    return any(
        isinstance(mode, dict)
        and mode.get("id") == "mad"
        and isinstance(mode.get("scale"), (int, float))
        and abs(scale - mode["scale"]) < 1e-6
        for mode in modes
    )
