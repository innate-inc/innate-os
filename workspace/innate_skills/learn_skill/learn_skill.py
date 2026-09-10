# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""learn_skill: the robot writes a new skill for itself, tries it, and keeps it if the trial passes."""

from __future__ import annotations

import json
import os
from pathlib import Path

from innate_skills.learn_skill.forge import Forge, ForgeUnreachable, extract_code, system_prompt
from innate_skills.learn_skill.gate import Draft, DraftRejected, check
from innate_skills.learn_skill.performance import LearningMode

from brain_client.common.script_paths import LEARNED_GROUP, get_learned_skills_dir
from innate import Skill, SkillReturn
from innate_proxy import ProxyClient

ROUNDS = 3
ROSTER_TIMEOUT_S = (
    90.0  # the watcher's 1 s debounce plus a full workspace re-import (over a minute on a starved machine)
)
TRIAL_TIMEOUT_S = 60.0
# The skills server rewrites its contracts cache on every roster rebuild: the "your file is loaded" signal.
CONTRACTS = Path(os.environ.get("INNATE_SKILL_CACHE", "/tmp/innate_skill_contracts.json"))


class LearnSkill(Skill):
    """Write a NEW skill for something you have no tool for: a dance, a beep melody or sound,
    a phrase routine, a head or arm gesture, or a combination of existing skills. Give a precise
    description of what it should do and when to use it. Takes a minute or two; tell the user
    you are learning it first. The new skill appears in your tools when this completes."""

    def guidelines_when_running(self) -> str:
        return (
            "You are learning a new skill; it takes a minute or two. Answer the user briefly if "
            "they talk, do not narrate progress, and do not stop it unless asked."
        )

    def execute(self, description: str) -> SkillReturn:
        client = ProxyClient()
        if not client.is_available():
            self.fail("Innate proxy not configured (INNATE_SERVICE_KEY)")
        forge = Forge(client, system_prompt())
        prompt = f"Write a skill: {description}"
        written: set[Path] = set()  # this run's drafts; whatever never passes its trial is deleted
        draft: Draft | None = None
        problem: str | None = None
        try:
            with LearningMode(self) as show:
                for round_number in range(1, ROUNDS + 1):
                    self.feedback(f"round {round_number}: drafting")
                    try:
                        draft = check(self._draft(forge, show, prompt))
                        problem = self._install(draft, written) or self._trial(draft)
                    except (DraftRejected, ForgeUnreachable) as failure:
                        problem = str(failure)
                    if problem is None and draft is not None:
                        written.discard(_learned_path(draft))
                        show.celebrate(draft.display_name)
                        return f"Learned {draft.skill_id}: it is now one of your tools."
                    self.feedback(f"round {round_number} failed: {problem}")
                    prompt = f"That failed: {problem}\nFix it and reply with the complete file again."
        finally:
            for path in written:  # every draft of this run that did not pass its trial
                path.unlink(missing_ok=True)
        self.fail(f"Could not learn it after {ROUNDS} rounds: {problem}")

    def _draft(self, forge: Forge, show: LearningMode, prompt: str) -> str:
        reply: list[str] = []
        pending = ""
        for delta in forge.ask(prompt):
            self.check_cancelled()
            reply.append(delta)
            *lines, pending = (pending + delta).split("\n")
            for line in lines:
                show.mutter(line)
        return extract_code("".join(reply))

    def _install(self, draft: Draft, written: set[Path]) -> str | None:
        """Write the draft where the catalog looks and wait for the roster to rebuild."""
        path = _learned_path(draft)
        if path.exists() and path not in written:
            raise DraftRejected(f"a learned skill named {draft.class_name} already exists; choose another class name")
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        roster_before = _roster_stamp()
        try:
            staging.write_text(draft.source)
            staging.replace(path)  # atomic: the watcher never imports a half-written file
        finally:
            staging.unlink(missing_ok=True)
        written.add(path)
        loaded = self.wait_for(
            lambda: _roster_lists(draft) if _roster_stamp() != roster_before else None, timeout=ROSTER_TIMEOUT_S
        )
        return None if loaded else "the skill catalog did not pick the file up in time"

    def _trial(self, draft: Draft) -> str | None:
        if self.skills is None:
            self.fail("skill invoker unavailable")
        self.feedback(f"trying {draft.skill_id}")
        outcome = self.skills.run(draft.skill_id, timeout=TRIAL_TIMEOUT_S)
        return None if outcome.ok else outcome.message


def _learned_path(draft: Draft) -> Path:
    return get_learned_skills_dir() / f"{draft.module}.py"


def _roster_stamp() -> int:
    return CONTRACTS.stat().st_mtime_ns if CONTRACTS.exists() else 0


def _roster_lists(draft: Draft) -> bool | None:
    """True once the rebuilt roster carries the draft's skill, or its module's load error."""
    try:
        ids = {skill["id"] for skill in json.loads(CONTRACTS.read_text())["skills"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return True if draft.skill_id in ids or f"local/{LEARNED_GROUP}.{draft.module}" in ids else None
