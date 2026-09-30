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
from innate import MainImage, Skill, SkillOutput, SkillReturn
from innate_proxy import ProxyClient

ROUNDS = 3
ROSTER_TIMEOUT_S = (
    90.0  # the watcher's 1 s debounce plus a full workspace re-import (over a minute on a starved machine)
)
TRIAL_TIMEOUT_S = 60.0
# The skills server rewrites its contracts cache on every roster rebuild: the "your file is loaded" signal.
CONTRACTS = Path(os.environ.get("INNATE_SKILL_CACHE", "/tmp/innate_skill_contracts.json"))
T = TypeVar("T")


class RoundFailed(Exception):
    """The draft did not install or did not run; the message goes back to the coder."""


class LearnSkill(Skill):
    """Write a NEW skill for something you have no tool for: a dance, a beep melody or sound,
    a phrase routine, a head or arm gesture, or a combination of existing skills. Give a precise
    description of what it should do and when to use it. Takes a minute or two; tell the user
    you are learning it first. The new skill appears in your tools when this completes, and the
    result tells you what its trial did: judge that against what was asked. To fix a skill you
    learned earlier that failed or did the wrong thing, pass improve=<its tool name> and describe
    what went wrong: the fixed version replaces it, or the old one stays if no fix passes."""

    image: MainImage | None

    def guidelines_when_running(self) -> str:
        return (
            "You are learning a new skill; it takes a minute or two. Answer the user briefly if "
            "they talk, do not narrate progress, and do not stop it unless asked. When the update "
            "says it is trying the skill, watch what the robot does: you judge the result after."
        )

    def execute(self, description: str, improve: str = "") -> SkillReturn:
        client = ProxyClient()
        if not client.is_available():
            self.fail("Innate proxy not configured (INNATE_SERVICE_KEY)")
        try:
            coder = Coder.from_env()
        except ValueError as misconfigured:
            self.fail(str(misconfigured))
        forge = Forge(client, coder, system_prompt())
        _drop_orphan_drafts()
        target = self._improving(improve)
        prompt = _improve_prompt(target, description) if target else f"Write a skill: {description}"
        written: set[Path] = set()  # this run's drafts; whatever never passes its trial is deleted or put back
        problem = ""
        if target:
            _backup(target).write_text(target.read_text())  # outlives a crash mid-trial: see _drop_orphan_drafts
            written.add(target)
        try:
            with LearningMode(self) as show:
                for round_number in range(1, ROUNDS + 1):
                    self.feedback(f"round {round_number}: drafting")
                    try:
                        reply = self._draft(forge, show, prompt)
                        if (reason := refusal(reply)) is not None:
                            self.fail(f"Not learnable with the robot's current interfaces: {reason}")
                        draft = check(extract_code(reply))
                        if target and draft.module != target.stem:
                            raise DraftRejected(f"keep the class name: the skill stays local/{target.stem}")
                        self._install(draft, written)
                        trial = self._trial(draft)
                    except (DraftRejected, ForgeUnreachable, RoundFailed) as failure:
                        problem = str(failure)
                        self.feedback(f"round {round_number} failed: {problem}")
                        prompt = f"That failed: {problem}\nFix it and reply with the complete file again."
                        continue
                    return self._keep(draft, written, show, trial, improved=target is not None)
        finally:
            for path in written:  # every draft of this run that did not pass its trial
                _restore_or_drop(path)
            if target:
                _backup(target).unlink(missing_ok=True)
        if target:
            self.fail(f"Could not improve {target.stem} after {ROUNDS} rounds: {problem}. The old version is back.")
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

    def _install(self, draft: Draft, written: set[Path]) -> None:
        """Write the draft, marked as on trial, where the catalog looks and wait for the roster to load it."""
        path = _learned_path(draft)
        if path not in written and (path.exists() or _roster_lists(draft)):
            raise DraftRejected(f"a skill named {draft.class_name} already exists; choose another class name")
        path.parent.mkdir(parents=True, exist_ok=True)
        written.add(path)
        source = f"{draft.source.rstrip()}\n{DRAFT_MARKER}"
        if not self._publish(path, source, lambda: _on_trial(draft)):
            raise RoundFailed("the skill catalog did not pick the file up in time")

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

    def _trial(self, draft: Draft) -> SkillOutput:
        """Run the draft once with no inputs: its output when it passes, RoundFailed with its reason otherwise."""
        if self.skills is None:
            self.fail("skill invoker unavailable")
        self.feedback(f"trying {draft.skill_id}")
        outcome = self.skills.run(draft.skill_id, timeout=TRIAL_TIMEOUT_S)
        if not outcome.ok:
            raise RoundFailed(outcome.message)
        return outcome

    def _keep(
        self, draft: Draft, written: set[Path], show: LearningMode, trial: SkillOutput, *, improved: bool
    ) -> SkillReturn:
        """Acquire the draft that passed and hand the brain what its trial did, so it can judge the result:
        the trial's own evidence image when it attached one, else what the head camera sees now."""
        advertised = self._acquire(draft, written)
        show.celebrate(draft.display_name, improved=improved)
        verb = "Improved" if improved else "Learned"
        listed = "it is now one of your tools" if advertised else "it joins your tools once the catalog reloads"
        return SkillOutput(
            f"{verb} {draft.skill_id}: {listed}. Its trial just ran and reported: {trial.message or 'nothing'}",
            image=trial.image or (self.image.jpeg if self.image else None),
        )

    def _improving(self, name: str) -> Path | None:
        """The learned skill an `improve` request names, by tool name or id; None writes a new one."""
        if not name:
            return None
        stem = name.removeprefix("local/")
        path = get_learned_skills_dir() / f"{stem}.py"
        # a module name only: no separators, so the name cannot reach a file outside learned/
        if not stem.isidentifier() or not path.is_file():
            self.fail(f"{name} is not a skill I learned, so I cannot rewrite it")
        return path


def _learned_path(draft: Draft) -> Path:
    return get_learned_skills_dir() / f"{draft.module}.py"


def _improve_prompt(target: Path, description: str) -> str:
    return (
        f"Improve a skill you wrote earlier. Its current file:\n```python\n{target.read_text()}```\n"
        f"When the robot ran it: {description}\nFix it, keep the class name, and reply with the complete file."
    )


def _backup(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak")  # not a .py: neither the catalog nor the watcher sees it


def _restore_or_drop(path: Path) -> None:
    """Put back the version an improvement replaced, or drop a draft that never was a skill."""
    backup = _backup(path)
    if backup.exists():
        backup.replace(path)
    else:
        path.unlink(missing_ok=True)


def _drop_orphan_drafts() -> None:
    """A draft on disk before this run began was left by a run that died mid-trial (one learns at a
    time): loaded but withheld, it would hide from the roster and hold its class name forever."""
    for stale in get_learned_skills_dir().glob("*.py"):
        if is_draft(stale):
            _restore_or_drop(stale)
    for orphan in get_learned_skills_dir().glob("*.py.bak"):
        orphan.unlink()


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
