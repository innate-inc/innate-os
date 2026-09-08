# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Skill


class SuggestUserPrompts(Skill):
    """Offer up to three short, contextual requests the user can click in chat.

    Use when waiting for the user's next instruction: after introducing a
    mission, after finishing everything they requested, when they ask for help,
    or after a failure to offer an explicit retry. Write in the user's voice,
    e.g. 'Try picking it up again' or 'Put it in the box'. These are optional
    suggestions, never commands to execute or requirements for progression.
    Do not interrupt an action or split an already-authorized sequence to offer
    suggestions. Do not suggest success before the scene confirms it. Pass []
    to clear obsolete suggestions. This skill does not reveal camera controls;
    the interface reveals Main, Arm, and third-person views when the first task starts.
    """

    def execute(self, prompts: list[str]) -> None:
        # The interface consumes the validated inputs on the completed status
        # event. No extra transport or spoken/internal output is needed.
        if not isinstance(prompts, list) or len(prompts) > 3:
            self.fail("Provide a list of zero to three short suggested requests.")
        if any(not isinstance(p, str) or not p.strip() or len(p) > 160 for p in prompts):
            self.fail("Each suggestion must be nonempty text of at most 160 characters.")
