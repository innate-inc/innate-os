# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Run-scoped visual identity without task-specific resident data."""

from innate_skills.resident_roster import ResidentRoster

from innate import SkillReturn


class PersonIdentity(ResidentRoster):
    """Create and recognize stable visual person IDs for the active run.

    ``begin`` starts fresh identity state, ``identify`` captures a new upward
    camera frame and matches it against saved references, and ``visualize``
    returns the current identity overview. Mission facts belong in a separate
    notes skill.
    """

    def execute(self, action: str, encounter_id: str | None = None) -> SkillReturn:
        normalized = action.strip().lower() if isinstance(action, str) else ""
        if normalized == "begin":
            return self._begin()
        if normalized == "identify":
            return self._identify(encounter_id)
        if normalized == "visualize":
            return self._visualize()
        self.fail("Unknown action. Use one of: begin, identify, visualize.")
