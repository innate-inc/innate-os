# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Declarative skill cues synchronized to one spoken response."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from brain_client.transport.playback import PlaybackEvent

if TYPE_CHECKING:
    from brain_client.brain.loop import LoopThread
    from brain_client.core.state import BrainState


class Logger(Protocol):
    def warning(self, message: str) -> None: ...


@dataclass(frozen=True)
class SkillCue:
    skill_id: str
    inputs: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechSequence:
    at_start: SkillCue | None = None
    near_end: SkillCue | None = None
    near_end_lead_seconds: float = 0.0


@dataclass(frozen=True)
class SpeechSequenceSession:
    _runner: SpeechSequenceRunner
    sequence: SpeechSequence
    generation: int

    def on_playback(self, event: PlaybackEvent) -> None:
        self._runner.on_playback(self, event)


class SpeechSequenceRunner:
    """Turns actual playback events into ordered, silent system skill runs."""

    def __init__(
        self,
        runtime: LoopThread,
        state: BrainState,
        start_skill: Callable[[str, dict[str, object], Callable[[], None]], bool],
        logger: Logger,
    ) -> None:
        self._runtime = runtime
        self._state = state
        self._start_skill = start_skill
        self._logger = logger
        self._generation = 0
        self._playback_started = False
        self._near_end_received = False
        self._start_running = False
        self._near_end_waiting = False

    def open(self, sequence: SpeechSequence) -> SpeechSequenceSession:
        self._generation += 1
        self._playback_started = False
        self._near_end_received = False
        self._start_running = False
        self._near_end_waiting = False
        return SpeechSequenceSession(self, sequence, self._generation)

    def cancel(self) -> None:
        self._generation += 1
        self._playback_started = False
        self._near_end_received = False
        self._start_running = False
        self._near_end_waiting = False

    def on_playback(self, session: SpeechSequenceSession, event: PlaybackEvent) -> None:
        self._runtime.post(self._handle_playback, session, event)

    def _handle_playback(self, session: SpeechSequenceSession, event: PlaybackEvent) -> None:
        if not self._current(session):
            return
        if event == PlaybackEvent.STARTED:
            self._playback_started = True
            self._start(session)
            if self._near_end_received:
                self._near_end(session)
        elif event == PlaybackEvent.NEAR_END:
            self._near_end_received = True
            if self._playback_started:
                self._near_end(session)
        elif event == PlaybackEvent.ABORTED:
            self.cancel()

    def _start(self, session: SpeechSequenceSession) -> None:
        cue = session.sequence.at_start
        if cue is None:
            return
        self._start_running = self._run(cue, lambda: self._start_finished(session))

    def _near_end(self, session: SpeechSequenceSession) -> None:
        if self._start_running:
            self._near_end_waiting = True
            return
        cue = session.sequence.near_end
        if cue is not None:
            self._run(cue, lambda: None)

    def _start_finished(self, session: SpeechSequenceSession) -> None:
        self._runtime.post(self._finish_start, session)

    def _finish_start(self, session: SpeechSequenceSession) -> None:
        if not self._current(session):
            return
        self._start_running = False
        if self._near_end_waiting:
            self._near_end_waiting = False
            self._near_end(session)

    def _current(self, session: SpeechSequenceSession) -> bool:
        return session.generation == self._generation and self._state.is_brain_active

    def _run(self, cue: SkillCue, on_finished: Callable[[], None]) -> bool:
        started = self._start_skill(cue.skill_id, dict(cue.inputs), on_finished)
        if not started:
            self._logger.warning(f"Skipping speech cue '{cue.skill_id}' because the skill slot is occupied")
        return started
