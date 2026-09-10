# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the platform-owned skill cancel latch (Skill._cancelled).

Pins the fix for the lost-cancel race: a cancel() that fires between goal
acceptance and execute() entry must survive the `self._cancelled = False`
reset that most skills perform as their first statement.
"""

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from brain_client.skills.types import (
    Interface,
    InterfaceType,
    Skill,
    SkillCancelled,
    SkillResult,
    cancellable_sleep,
    swap_run_cancel,
)


class LegacyPatternSkill(Skill):
    """Mirrors the pattern used by the existing skill fleet: own _cancelled
    flag, reset at execute() entry, set by an overridden cancel()."""

    def __init__(self):
        super().__init__(logging.getLogger("test"))
        self._cancelled = False

    @property
    def name(self):
        return "legacy_pattern"

    def execute(self):
        self._cancelled = False  # the reset that used to wipe an early cancel
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED
        return "Done", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "cancelled"


def _goal_handle(cancel_requested):
    return SimpleNamespace(is_cancel_requested=cancel_requested)


def test_cancel_before_execute_survives_reset():
    skill = LegacyPatternSkill()
    skill._begin_run(_goal_handle(False))
    skill.cancel()  # races ahead of execute() on another thread
    skill.execute()  # legacy reset must not clear the latch
    assert skill._cancelled is True


def test_begin_run_latches_cancel_from_goal_handle():
    # cancel landed before _begin_run (before the instance even existed): the
    # goal's persistent cancel status latches it at run start.
    skill = LegacyPatternSkill()
    skill._begin_run(_goal_handle(True))
    assert skill._cancelled is True


def test_setting_false_is_ignored_and_true_latches():
    skill = LegacyPatternSkill()
    skill._begin_run(None)
    skill._cancelled = False
    assert skill._cancelled is False
    skill._cancelled = True
    skill._cancelled = False  # mid-run reset attempts must not unlatch
    assert skill._cancelled is True


def test_base_cancel_latches_without_invoker():
    skill = LegacyPatternSkill()
    skill._begin_run(None)
    Skill.cancel(skill)  # base implementation, no self.skills set
    assert skill._cancelled is True


class NoSuperInitSkill(Skill):
    """Some fleet skills (navigate_to_position, …) define __init__
    without calling super().__init__() — the latch must self-create."""

    def __init__(self):
        # intentionally no super().__init__() — the latch must self-create
        self._cancelled = False

    @property
    def name(self):
        return "no_super_init"

    def execute(self):
        return "Done", SkillResult.SUCCESS


def test_latch_works_without_super_init():
    skill = NoSuperInitSkill()
    assert skill._cancelled is False
    skill._begin_run(_goal_handle(False))
    Skill.cancel(skill)
    assert skill._cancelled is True


class SleepySkill(Skill):
    """The new authoring shape: no cancel code, just self.sleep in loops."""

    def execute(self):
        while True:
            self.sleep(0.05)

    @property
    def name(self):
        return "sleepy"


def test_sleep_raises_immediately_once_cancelled():
    skill = SleepySkill(logging.getLogger("test"))
    skill.cancel()
    start = time.monotonic()
    with pytest.raises(SkillCancelled):
        skill.sleep(10.0)
    assert time.monotonic() - start < 1.0


def test_sleep_wakes_mid_wait_on_cancel():
    skill = SleepySkill(logging.getLogger("test"))
    threading.Timer(0.1, skill.cancel).start()
    start = time.monotonic()
    with pytest.raises(SkillCancelled):
        skill.execute()
    assert time.monotonic() - start < 5.0


def test_cancel_halts_injected_interfaces():
    class Braked(Skill):
        mobility = Interface(InterfaceType.MOBILITY)

        def execute(self):
            pass

    class FakeMobility:
        def __init__(self):
            self.halted = False

        def halt(self):
            self.halted = True

    skill = Braked(logging.getLogger("test"))
    base = FakeMobility()
    skill.inject_interface(InterfaceType.MOBILITY, base)
    skill.cancel()
    assert base.halted


def test_cancellable_sleep_follows_the_bound_run_latch():
    latch = threading.Event()
    previous = swap_run_cancel(latch)
    try:
        cancellable_sleep(0)  # not cancelled: returns
        latch.set()
        with pytest.raises(SkillCancelled):
            cancellable_sleep(10.0)
    finally:
        swap_run_cancel(previous)
