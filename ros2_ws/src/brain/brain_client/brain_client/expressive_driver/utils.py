# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure helpers for the expression driver — no ROS, no I/O."""

from __future__ import annotations

import json

# Skills that leave the body to this layer: express drives it through the driver, the rest never
# move it. Declared interfaces cannot prove a skill body-free (navigate_to_position and
# navigate_with_vision drive the base through raw ROS clients), so any other skill masks.
LEAVES_BODY = frozenset({"express", "search_memory", "change_volume"})


def leaves_body(skill_id: str) -> bool:
    return skill_id.rsplit("/", 1)[-1] in LEAVES_BODY


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
