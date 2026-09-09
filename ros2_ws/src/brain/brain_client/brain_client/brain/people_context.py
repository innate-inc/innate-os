# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The People line of a turn's observation: who the robot's memory says is in
the picture, and where they are standing in it.

A name is stated plainly; an outfit match is hedged out loud ("probably"),
because the model speaks these words and being wrong about a name costs more
than being vague. PURE module: no ROS, no I/O.
"""

from __future__ import annotations

MAX_LISTED = 4


def render(snapshot: dict) -> str | None:
    """The line, or None when there is nobody to say anything about."""
    people = [_describe(person) for person in snapshot.get("people", [])]
    listed = [text for text in people[:MAX_LISTED] if text]
    if not listed:
        return None
    rest = len(people) - len(listed)
    tail = f", and {rest} more" if rest > 0 else ""
    return f"People in view: {', '.join(listed)}{tail}."


def _describe(person: dict) -> str | None:
    where = str(person.get("where") or "ahead")
    name = str(person.get("name") or "")
    state = str(person.get("state") or "")
    if state == "known":
        return f"{name} ({where})" if name else f"someone you have met before ({where})"
    if state == "probable":
        seen = f"probably {name}" if name else "probably someone you have met before"
        return f"{seen}, going by their clothes ({where})"
    return f"someone you do not know ({where})"
