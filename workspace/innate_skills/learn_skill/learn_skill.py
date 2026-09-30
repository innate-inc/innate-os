# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""learn_skill: the robot writes a new skill for itself, tries it, and keeps it if the trial passes."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from innate_skills.learn_skill.forge import Coder, Forge, ForgeUnreachable, extract_code, refusal, system_prompt
from innate_skills.learn_skill.gate import Draft, DraftRejected, check
from innate_skills.learn_skill.performance import LearningMode

from brain_client.common.script_paths import DRAFT_MARKER, LEARNED_GROUP, get_learned_skills_dir, is_draft
from innate import Skill, SkillReturn
from innate_proxy import ProxyClient

ROUNDS = 3
ROSTER_TIMEOUT_S = (
    90.0  # the watcher's 1 s debounce plus a full workspace re-import (over a minute on a starved machine)
)
TRIAL_TIMEOUT_S = 60.0
# The skills server rewrites its contracts cache on every roster rebuild: the "your file is loaded" signal.
CONTRACTS = Path(os.environ.get("INNATE_SKILL_CACHE", "/tmp/innate_skill_contracts.json"))
T = TypeVar("T")


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
        try:
            coder = Coder.from_env()
        except ValueError as misconfigured:
            self.fail(str(misconfigured))
        forge = Forge(client, coder, system_prompt())
        prompt = f"Write a skill: {description}"
        written: set[Path] = set()  # this run's drafts; whatever never passes its trial is deleted
        draft: Draft | None = None
        problem: str | None = None
        _drop_orphan_drafts()
        try:
            with LearningMode(self) as show:
                for round_number in range(1, ROUNDS + 1):
                    self.feedback(f"round {round_number}: drafting")
                    try:
                        reply = self._draft(forge, show, prompt)
                        if (reason := refusal(reply)) is not None:
                            self.fail(f"Not learnable with the robot's current interfaces: {reason}")
                        draft = check(extract_code(reply))
                        problem = self._install(draft, written) or self._trial(draft)
                    except (DraftRejected, ForgeUnreachable) as failure:
                        problem = str(failure)
                    if problem is None and draft is not None:
                        advertised = self._acquire(draft, written)
                        show.celebrate(draft.display_name)
                        if advertised:
                            return f"Learned {draft.skill_id}: it is now one of your tools."
                        return (
                            f"Learned {draft.skill_id}; it joins your tools once the skill catalog finishes reloading."
                        )
                    self.feedback(f"round {round_number} failed: {problem}")
                    prompt = f"That failed: {problem}\nFix it and reply with the complete file again."
        finally:
            for path in written:  # every draft of this run that did not pass its trial
                path.unlink(missing_ok=True)
        self.fail(f"Could not learn it after {ROUNDS} rounds: {problem}")

    def _draft(self, forge: Forge, show: LearningMode, prompt: str) -> str:
        """The coder's whole reply, with the show's beeps for company while it streams."""
        reply: list[str] = []
        with show.drafting() as drafted:
            for delta in forge.ask(prompt):
                self.check_cancelled()
                reply.append(delta)
                drafted(len(delta))
        return "".join(reply)

    def _install(self, draft: Draft, written: set[Path]) -> str | None:
        """Write the draft, marked as on trial, where the catalog looks and wait for the roster to load it."""
        path = _learned_path(draft)
        if path not in written and (path.exists() or _roster_lists(draft)):
            raise DraftRejected(f"a skill named {draft.class_name} already exists; choose another class name")
        path.parent.mkdir(parents=True, exist_ok=True)
        written.add(path)
        source = f"{draft.source.rstrip()}\n{DRAFT_MARKER}"
        loaded = self._publish(path, source, lambda: _on_trial(draft))
        return None if loaded else "the skill catalog did not pick the file up in time"

    def _acquire(self, draft: Draft, written: set[Path]) -> bool:
        """Drop the trial marker so the roster advertises the skill; it passed its trial, so the file
        leaves this run's cleanup. False when the rebuild is still running at the deadline: the file
        is final and will be listed, but it is not a tool yet. A final load that fails ends the run."""
        path = _learned_path(draft)
        written.discard(path)
        entry = self._publish(path, draft.source, lambda: _settled(draft))
        if entry is not None and entry.get("load_error"):
            written.add(path)  # a file that no longer loads is not kept
            self.fail(f"{draft.skill_id} passed its trial but failed to load once acquired: {entry['load_error']}")
        return entry is not None

    def _publish(self, path: Path, source: str, settled: Callable[[], T | None]) -> T | None:
        staging = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        roster_before = _roster_stamp()
        try:
            staging.write_text(source)
            staging.replace(path)  # atomic: the watcher never imports a half-written file
        finally:
            staging.unlink(missing_ok=True)
        return self.wait_for(lambda: settled() if _roster_stamp() != roster_before else None, timeout=ROSTER_TIMEOUT_S)

    def _trial(self, draft: Draft) -> str | None:
        if self.skills is None:
            self.fail("skill invoker unavailable")
        self.feedback(f"trying {draft.skill_id}")
        outcome = self.skills.run(draft.skill_id, timeout=TRIAL_TIMEOUT_S)
        return None if outcome.ok else outcome.message


def _learned_path(draft: Draft) -> Path:
    return get_learned_skills_dir() / f"{draft.module}.py"


def _drop_orphan_drafts() -> None:
    """A draft on disk before this run began was left by a run that died mid-trial (one learns at a
    time): loaded but withheld, it would hide from the roster and hold its class name forever."""
    for stale in get_learned_skills_dir().glob("*.py"):
        if is_draft(stale):
            stale.unlink()


def _roster_stamp() -> int:
    return CONTRACTS.stat().st_mtime_ns if CONTRACTS.exists() else 0


def _roster() -> list[dict]:
    try:
        return json.loads(CONTRACTS.read_text())["skills"]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _roster_lists(draft: Draft) -> bool:
    """A skill the draft would shadow: the same id (a user's custom skill) or the same tool name (a
    shipped one, which `local/` would outrank). A draft on the roster is never acquired, so it is not."""
    return any(
        not skill.get("draft") and (skill["id"] == draft.skill_id or skill.get("name") == draft.module)
        for skill in _roster()
    )


def _roster_entry(draft: Draft) -> dict | None:
    """The roster's entry for the draft's file: its skill, or its module's load error."""
    # a module that never imported clean is keyed by its path; one that did keeps its class id
    ids = (draft.skill_id, f"local/{LEARNED_GROUP}.{draft.module}")
    return next((skill for skill in _roster() if skill["id"] in ids), None)


def _on_trial(draft: Draft) -> bool | None:
    """True once the rebuilt roster carries the draft on trial, or its load error for the trial to report."""
    entry = _roster_entry(draft)
    return True if entry is not None and (entry.get("draft") or entry.get("load_error")) else None


def _settled(draft: Draft) -> dict | None:
    """The acquired file's entry once the rebuilt roster has it: listed as a plain skill, or broken."""
    entry = _roster_entry(draft)
    return entry if entry is not None and not entry.get("draft") else None
