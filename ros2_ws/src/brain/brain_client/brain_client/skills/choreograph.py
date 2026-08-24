# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, cast


class Performable(Protocol):
    def __call__(self, *, timeout: float | None = None, **inputs: object) -> object: ...


class CodePerformable(Protocol):
    def __call__(self, **inputs: object) -> object: ...


class ChoreographyHost(Protocol):
    def say(self, text: str, *, wait: bool = False) -> object: ...
    def check_cancelled(self) -> None: ...
    def wait_for(self, read: Callable[[], object | None], timeout: float, poll: float = 0.02) -> object | None: ...
    def cancel(self) -> None: ...
    def _start_choreographed_speech(self, text: str) -> bool: ...
    def _wait_for_choreographed_speech_end(self, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Say:
    text: str


@dataclass(frozen=True, slots=True)
class Perform:
    handle: Performable
    timeout: float | None = None
    start_after: float = 0.0
    inputs: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Together:
    speech: Say
    actions: tuple[Perform, ...]


ChoreographyStep = Say | Perform | Together


def say(text: str) -> Say:
    if not text.strip():
        raise ValueError("say() requires non-empty text")
    return Say(text)


def perform(
    handle: Performable,
    /,
    *,
    timeout: float | None = None,
    start_after: float = 0.0,
    **inputs: object,
) -> Perform:
    if not callable(handle) or not (hasattr(handle, "execute") or _is_physical(handle)):
        raise TypeError("perform() requires a wired skill attribute such as self.motion")
    if timeout is not None and timeout <= 0:
        raise ValueError("perform() timeout must be greater than zero")
    if timeout is not None and not _is_physical(handle):
        raise TypeError("perform() timeout is only supported for physical skills")
    if start_after < 0:
        raise ValueError("perform() start_after must be zero or greater")
    return Perform(handle, timeout, start_after, MappingProxyType(dict(inputs)))


def together(*steps: Say | Perform) -> Together:
    speeches = tuple(step for step in steps if isinstance(step, Say))
    actions = tuple(step for step in steps if isinstance(step, Perform))
    if len(speeches) != 1 or not actions or len(speeches) + len(actions) != len(steps):
        raise TypeError("together() requires exactly one say() and one or more perform() steps")
    if sum(_is_physical(action.handle) for action in actions) > 1:
        raise TypeError("together() accepts at most one physical skill")
    return Together(speeches[0], actions)


def _is_physical(handle: Performable) -> bool:
    return bool(getattr(handle, "_is_bound_physical_skill", False))


def run(skill: ChoreographyHost, steps: tuple[ChoreographyStep, ...]) -> None:
    for step in steps:
        if isinstance(step, Say):
            skill.say(step.text, wait=True)
            skill.check_cancelled()
            continue
        if isinstance(step, Perform):
            _perform_after(skill, step, threading.Event())
            continue
        if not isinstance(step, Together):
            raise TypeError("choreograph() accepts only say(), perform(), and together() steps")

        speech_started = skill._start_choreographed_speech(step.speech.text)
        _perform_together(skill, step.actions)
        if speech_started:
            skill._wait_for_choreographed_speech_end(step.speech.text)
        skill.check_cancelled()


def _perform(step: Perform) -> None:
    if _is_physical(step.handle):
        step.handle(timeout=step.timeout, **step.inputs)
    else:
        cast(CodePerformable, step.handle)(**step.inputs)


def _perform_together(skill: ChoreographyHost, actions: tuple[Perform, ...]) -> None:
    if len(actions) == 1:
        _perform_after(skill, actions[0], threading.Event())
        return

    failed = threading.Event()
    errors: list[Exception] = []
    error_lock = threading.Lock()

    def run_action(action: Perform) -> None:
        try:
            _perform_after(skill, action, failed)
        except Exception as error:  # noqa: BLE001 — transfer action-thread failures to the skill runner
            with error_lock:
                errors.append(error)
            failed.set()
            skill.cancel()

    threads = tuple(threading.Thread(target=run_action, args=(action,), daemon=True) for action in actions)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


def _perform_after(skill: ChoreographyHost, action: Perform, failed: threading.Event) -> None:
    if action.start_after:
        stopped = skill.wait_for(lambda: True if failed.is_set() else None, timeout=action.start_after)
        if stopped:
            return
    skill.check_cancelled()
    _perform(action)
