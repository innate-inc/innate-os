# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The show around learning: the announcement, drafted code muttered fast and low with beeps
between the lines, and the acquired jingle."""

from __future__ import annotations

import math
import random
import re
import threading
import time
from array import array
from collections import deque
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from innate import Skill

SAMPLE_RATE = 16000
AMPLITUDE = 8000
ATTACK_SAMPLES = SAMPLE_RATE // 200
MUTTER_SPEED = 1.5
MUTTER_VOLUME = 0.5
BEEP_GAP_S = (7.0, 13.0)
BEAT_S = 0.14
NOTE_HZ = {
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
BEEP_NOTES = ("C5", "D5", "E5", "G5", "A5")
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
_WORD = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?")


class LearningMode:
    """Announces on enter, mutters every line handed to :meth:`mutter` in a fast low voice with
    beeps in the gaps until exit, and :meth:`celebrate`s a success in the skill's own voice."""

    def __init__(self, skill: Skill):
        self._skill = skill
        self._lines: deque[str] = deque(maxlen=12)  # the model outruns speech: mutter the freshest lines
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="learning-mode", daemon=True)

    def __enter__(self) -> LearningMode:
        self._skill.say("Activating learning mode. Give me a moment.", wait=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._quiet()

    def mutter(self, line: str) -> None:
        words = _WORD.findall(re.sub(r"([a-z])([A-Z])", r"\1 \2", line.split("#", 1)[0]))
        if len(words) >= 2:
            self._lines.append(" ".join(words).lower())

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
            if time.monotonic() >= next_beep:
                self._skill.play_clip(
                    _synth([(note, 0.65) for note in random.sample(BEEP_NOTES, random.randint(2, 3))])
                )
                next_beep = time.monotonic() + random.uniform(*BEEP_GAP_S)
            if self._lines:
                self._skill.say(self._lines.popleft(), wait=True, speed=MUTTER_SPEED, volume=MUTTER_VOLUME)
            else:
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
