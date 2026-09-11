# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The show around learning: the announcement, beeps climbing as a draft streams, and the acquired jingle."""

from __future__ import annotations

import math
import random
import threading
import time
from array import array
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from innate import Skill

SAMPLE_RATE = 16000
AMPLITUDE = 8000
ATTACK_SAMPLES = SAMPLE_RATE // 200
BEEP_GAP_S = (1.2, 2.0)
BEAT_S = 0.14
NOTE_HZ = {
    "C4": 261.63,
    "D4": 293.66,
    "E4": 329.63,
    "G4": 392.0,
    "A4": 440.0,
    "C5": 523.25,
    "D5": 587.33,
    "E5": 659.25,
    "G5": 783.99,
    "A5": 880.0,
    "B5": 987.77,
    "C6": 1046.5,
    "D6": 1174.66,
    "E6": 1318.51,
    "G6": 1567.98,
}
# Two pentatonic octaves the draft climbs a rung at a time: where the beep sits is how far along it is.
BEEP_LADDER = ("C4", "D4", "E4", "G4", "A4", "C5", "D5", "E5", "G5", "A5", "C6", "D6", "E6", "G6")
DRAFT_CHARS = 3000  # a learned skill file runs 1.4-3.1 kB; a longer one holds the top rung
# The acquired fanfare: a quick climb, three pushes up to the tonic, a turn, and a held major chord.
JINGLE = (
    ("C5", 0.5),
    ("E5", 0.5),
    ("G5", 0.5),
    ("C6", 1.0),
    ("G5", 1.0),
    ("A5", 1.0),
    ("B5", 1.0),
    ("C6", 2.0),
    ("E6", 0.5),
    ("D6", 0.5),
    ("C6", 0.5),
    ("C6 E6 G6", 3.0),
)


class LearningMode:
    """Announces on enter, beeps up the ladder as a draft streams (:meth:`drafting`), and
    :meth:`celebrate`s a success in the skill's own voice."""

    def __init__(self, skill: Skill):
        self._skill = skill
        self._drafted = 0
        self._drafting = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="learning-mode", daemon=True)

    def __enter__(self) -> LearningMode:
        self._skill.say("Activating learning mode. Give me a moment.", wait=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._quiet()

    @contextmanager
    def drafting(self) -> Iterator[Callable[[int], None]]:
        """Beeps climb the ladder as the coder streams and stop with it, so the trial that follows
        performs in its own voice. Feed the yielded call every character that arrives."""
        self._drafted = 0
        self._drafting.set()
        try:
            yield self._advance
        finally:
            self._drafting.clear()

    def _advance(self, chars: int) -> None:
        self._drafted += chars

    def _rung(self) -> str:
        return BEEP_LADDER[min(len(BEEP_LADDER) * self._drafted // DRAFT_CHARS, len(BEEP_LADDER) - 1)]

    def celebrate(self, display_name: str) -> None:
        """The fanfare and the line, in that order, from the robot's own speaker."""
        self._quiet()
        self._skill.play_clip(_synth(JINGLE), "level-up fanfare")
        self._skill.say(f"New skill: {display_name}. Acquired.", wait=True)

    def _quiet(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        next_beep = time.monotonic() + random.uniform(*BEEP_GAP_S)
        while not self._stop.is_set():
            if not self._drafting.wait(0.2):
                next_beep = time.monotonic() + random.uniform(*BEEP_GAP_S)
                continue
            if time.monotonic() >= next_beep:
                self._skill.play_clip(_synth([(self._rung(), 1.0)]))
                next_beep = time.monotonic() + random.uniform(*BEEP_GAP_S)
            self._stop.wait(0.2)


def _synth(score: Sequence[tuple[str, float]]) -> bytes:
    """Chiptune rendering of ``(notes, beats)`` steps as 16-bit mono PCM: square waves, a chord
    per step when the notes are space-separated, each step plucked (fast attack, settling decay)."""
    samples = array("h")
    for names, beats in score:
        voices = [SAMPLE_RATE / NOTE_HZ[name] for name in names.split()]
        length = int(SAMPLE_RATE * beats * BEAT_S)
        for i in range(length):
            envelope = min(1.0, i / ATTACK_SAMPLES) * (0.35 + 0.65 * math.exp(-4.0 * i / length))
            square = sum(1 if i % period < period / 2 else -1 for period in voices) / len(voices)
            samples.append(int(AMPLITUDE * envelope * square))
    return samples.tobytes()
