# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import People, Skill, SkillReturn


class ForgetPerson(Skill):
    """Forget a person completely, on their own request or the owner's: their face,
    their name, everything ever noted about them, and every encounter. Give the tag
    from the People block ('P3'), their name ('Ana'), or their person id. This
    cannot be undone and it is not a way to correct a mistake -- run it only when
    someone actually asked ('forget me', 'delete what you know about Ana'), and tell
    them afterwards that it is done."""

    people: People

    def execute(self, who: str) -> SkillReturn:
        person = self.people.find(who)
        # Address the record, not the word: a name is not unique and a tag dies
        # with the track, so the id wins whenever the person is in view.
        target = (person.person_id or person.tag) if person is not None else who
        done, message = self.people.forget(target)
        if not done:
            self.fail(message or f"I don't know anyone called {who}.")
        return message or f"Done — I've forgotten {who}."
