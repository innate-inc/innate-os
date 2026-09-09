# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import re

from innate import People, Skill, SkillReturn

_TAG = re.compile(r"^P\d+$", re.IGNORECASE)


class ForgetPerson(Skill):
    """Forget a person completely, on their own request or the owner's: their face,
    their name, everything ever noted about them, and every encounter. Give the tag
    from the People block ('P3'), their name ('Ana'), or their person id. This
    cannot be undone and it is not a way to correct a mistake -- run it only when
    someone actually asked ('forget me', 'delete what you know about Ana'), and tell
    them afterwards that it is done."""

    people: People

    def execute(self, who: str) -> SkillReturn:
        # A tag is handed over as the view it was read from, so the node can refuse
        # it once the track has changed hands; a name or id goes through untouched,
        # since only the node can see that two people answer to one name.
        target = self.people.find(who) if _TAG.match(who.strip()) else None
        done, message = self.people.forget(target or who)
        if not done:
            self.fail(message or f"I don't know anyone called {who}.")
        return message or f"Done — I've forgotten {who}."
