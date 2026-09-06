# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Signal an interaction that cannot proceed; participating agents choose the release policy."""

import json

from innate import Skill, SkillOutput, SkillReturn


class AbandonInteraction(Skill):
    """Abandon a pre-greeting interaction when the visible target cannot be grounded.

    Only before initiating dialogue or requesting confirmation, with no reply pending.
    This signals a guard release; it does not move, erase identity/notes, or cancel
    a request. Wait for completion, then search and obtain a fresh identity before
    another greeting. Never use it to skip confirmed facts that have not been saved.
    """

    def execute(self, reason: str) -> SkillReturn:
        if reason != "visual_target_unavailable":
            self.fail("reason must be visual_target_unavailable")
        self.check_cancelled()
        data = {"reason": reason}
        return SkillOutput(f"INTERACTION_ABANDONED {json.dumps(data, separators=(',', ':'))}", data=data)
