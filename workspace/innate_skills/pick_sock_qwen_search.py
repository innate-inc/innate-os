# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.pick_sock_qwen import PickSockQwen

from innate import SkillReturn


class PickSockQwenSearch(PickSockQwen):
    """Find and pick a floor sock, turning in place to search if necessary."""

    _p = {
        **PickSockQwen._p,
        "search_turns_deg": (0, -90, -90, -90),
        "silent_search": True,
    }

    def execute(self, prompt: str = "a sock on the floor outside the box") -> SkillReturn:
        """Find and pick a sock. Describe its color if visible; otherwise use the default search."""
        return super().execute(prompt)

    def _detection_question(self, selection):
        return super()._detection_question(selection) + " Exclude every sock inside a box."
