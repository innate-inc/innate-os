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
        done, message = self.people.forget(self._target(who))
        if not done:
            self.fail(message or f"I don't know anyone called {who}.")
        return message or f"Done — I've forgotten {who}."

    def _target(self, who: str) -> str:
        """A tag stands for one track, so address the record behind it -- the tag
        dies with the track, the id does not. Anything else goes through
        untouched: only the node can see that two people answer to one name."""
        if not _TAG.match(who.strip()):
            return who
        person = self.people.find(who)
        return (person.person_id or who) if person is not None else who
