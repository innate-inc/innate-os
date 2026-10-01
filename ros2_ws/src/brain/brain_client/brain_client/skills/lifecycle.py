# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared primitive lifecycle vocabulary and substep-feedback encoding."""

from __future__ import annotations

import json

PRIMITIVE_LIFECYCLE_STATUSES = {"running", "completed", "interrupted", "failed"}
PRIMITIVE_TERMINAL_STATUSES = frozenset(PRIMITIVE_LIFECYCLE_STATUSES - {"running"})

# Tags a chained child's start/finish smuggled through the parent's feedback
# stream (children run on the parent's goal, so they have no lifecycle of
# their own); PrimitiveRunner._on_feedback_msg decodes it back into a step
# status. ASCII Record Separator — never appears in real feedback text. Must
# not contain NUL: rclpy's C typesupport copies strings with strlen, so a
# NUL-prefixed feedback string arrives at subscribers truncated to "".
SUBSTEP_FEEDBACK_SENTINEL = "\x1esubstep\x1e"


def encode_substep_feedback(
    *,
    event: str,
    name: str,
    primitive_id: str,
    skill_id: str,
    reason: str | None = None,
    output: str | None = None,
) -> str:
    """Pack a child step's lifecycle event into a tagged feedback string."""
    payload = {"event": event, "name": name, "primitive_id": primitive_id, "skill_id": skill_id}
    if reason:
        payload["reason"] = reason
    if output:
        payload["output"] = output
    return SUBSTEP_FEEDBACK_SENTINEL + json.dumps(payload)


def decode_substep_feedback(text: str) -> dict | None:
    """Unpack a substep marker, or None if this is just normal feedback."""
    if not text or not text.startswith(SUBSTEP_FEEDBACK_SENTINEL):
        return None
    try:
        return json.loads(text[len(SUBSTEP_FEEDBACK_SENTINEL) :])
    except (ValueError, TypeError):
        return None
