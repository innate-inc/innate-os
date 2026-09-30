# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The show around learning: the announcement, a phrase that climbs further as a draft streams, and the
acquired jingle."""

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
# A lone note every second or two reads as a countdown; a rising phrase every few seconds reads as
# progress. Long enough apart that the phrase, not the interval, is what the ear follows.
PHRASE_GAP_S = (3.0, 4.5)
PHRASE_FLOOR = 3  # notes in the phrase before any progress: a chord run, never a single tick
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
# Two pentatonic octaves the draft climbs a rung at a time: how far up the phrase runs is how far along it is.
LADDER = ("C4", "D4", "E4", "G4", "A4", "C5", "D5", "E5", "G5", "A5", "C6", "D6", "E6", "G6")
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
    """Announces on enter, plays a phrase that climbs further up the ladder as a draft streams
    (:meth:`drafting`), and :meth:`celebrate`s a success in the skill's own voice."""

    def __init__(self, skill: Skill):
        self._skill = skill
        self._drafted = 0
        self._drafting = threading.Event()
        self._playing = threading.Lock()  # held while a phrase plays: drafting() ends only once it has
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
        """The phrase climbs the ladder as the coder streams and stops with it, so the trial that
        follows performs in its own voice. Feed the yielded call every character that arrives."""
        self._drafted = 0
        self._drafting.set()
        try:
            yield self._advance
        finally:
            self._drafting.clear()
            with self._playing:
                pass  # a phrase queued at the last moment would otherwise play into the trial

    def _advance(self, chars: int) -> None:
        self._drafted += chars

    def _phrase(self) -> bytes:
        """The ladder from its foot up to the rung the draft has reached, the top note held."""
        rung = min(len(LADDER) * self._drafted // DRAFT_CHARS, len(LADDER) - 1)
        notes = LADDER[: max(PHRASE_FLOOR, rung + 1)]
        return _synth([*((note, 0.5) for note in notes[:-1]), (notes[-1], 2.0)], soft=True)

    def celebrate(self, display_name: str, *, improved: bool = False) -> None:
        """The fanfare and the line, in that order, from the robot's own speaker."""
        self._quiet()
        self._skill.play_clip(_synth(JINGLE), "level-up fanfare")
        line = f"Skill: {display_name}. Improved." if improved else f"New skill: {display_name}. Acquired."
        self._skill.say(line, wait=True)

    def _quiet(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        next_phrase = time.monotonic()  # the first phrase opens the drafting, no gap
        while not self._stop.is_set():
            if not self._drafting.wait(0.2):
                next_phrase = time.monotonic()
                continue
            if time.monotonic() >= next_phrase:
                with self._playing:
                    if self._drafting.is_set():
                        self._skill.play_clip(self._phrase(), wait=True)
                next_phrase = time.monotonic() + random.uniform(*PHRASE_GAP_S)
            self._stop.wait(0.2)


def _synth(score: Sequence[tuple[str, float]], soft: bool = False) -> bytes:
    """Rendering of ``(notes, beats)`` steps as 16-bit mono PCM: chiptune square waves, or sines
    for the softer learning phrase; a chord per step when the notes are space-separated, each step
    plucked (fast attack, settling decay)."""
    samples = array("h")
    for names, beats in score:
        voices = [SAMPLE_RATE / NOTE_HZ[name] for name in names.split()]
        length = int(SAMPLE_RATE * beats * BEAT_S)
        for i in range(length):
            envelope = min(1.0, i / ATTACK_SAMPLES) * (0.35 + 0.65 * math.exp(-4.0 * i / length))
            if soft:
                tone = sum(math.sin(math.tau * i / period) for period in voices) / len(voices)
            else:
                tone = sum(1 if i % period < period / 2 else -1 for period in voices) / len(voices)
            samples.append(int(AMPLITUDE * envelope * tone))
    return samples.tobytes()
