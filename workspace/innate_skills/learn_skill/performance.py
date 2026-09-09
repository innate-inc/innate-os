# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The show around learning: the announcement, drafted code muttered fast and low with beeps
between the lines, and the acquired jingle."""

from __future__ import annotations

import base64
import io
import random
import re
import subprocess
import threading
import time
import wave
from array import array
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from std_msgs.msg import String

if TYPE_CHECKING:
    from innate import Skill

SAMPLE_RATE = 16000
MUTTER_SPEED = 1.5
MUTTER_VOLUME = 0.5
BEEP_GAP_S = (7.0, 13.0)
NOTES_HZ = (523.25, 587.33, 659.25, 783.99, 880.0)
# The sim container has no audio device: the browser plays /tts/audio (same check as the innate CLI).
IN_SIM = Path("/.dockerenv").exists()
_WORD = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?")


class LearningMode:
    """Announces on enter, mutters every line handed to :meth:`mutter` in a fast low voice with
    beeps in the gaps until exit, and :meth:`celebrate`s a success in the skill's own voice."""

    def __init__(self, skill: Skill):
        self._skill = skill
        self._lines: deque[str] = deque(maxlen=12)  # the model outruns speech: mutter the freshest lines
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="learning-mode", daemon=True)
        self._audio_pub = None

    def __enter__(self) -> LearningMode:
        if IN_SIM and self._skill.node is not None:
            self._audio_pub = self._skill.node.create_publisher(String, "/tts/audio", 10)
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
        self._quiet()
        self._play(_tone([NOTES_HZ[0], NOTES_HZ[2], NOTES_HZ[4], 2 * NOTES_HZ[0]], 0.12))
        self._skill.say(f"New skill: {display_name}. Acquired.", wait=True)

    def _quiet(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        next_beep = time.monotonic() + random.uniform(*BEEP_GAP_S)
        while not self._stop.is_set():
            if time.monotonic() >= next_beep:
                self._play(_tone(random.sample(NOTES_HZ, random.randint(2, 3)), 0.09))
                next_beep = time.monotonic() + random.uniform(*BEEP_GAP_S)
            if self._lines:
                self._skill.say(self._lines.popleft(), wait=True, speed=MUTTER_SPEED, volume=MUTTER_VOLUME)
            else:
                self._stop.wait(0.2)

    def _play(self, pcm: bytes) -> None:
        if self._audio_pub is not None:
            self._audio_pub.publish(String(data=base64.b64encode(_wav(pcm)).decode("ascii")))
            return
        subprocess.run(
            ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1"],
            input=pcm,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )


def _tone(notes_hz: list[float], note_s: float) -> bytes:
    """Square-wave notes back to back, 16-bit mono PCM."""
    samples = array("h")
    for hz in notes_hz:
        period = SAMPLE_RATE / hz
        samples.extend(6000 if i % period < period / 2 else -6000 for i in range(int(SAMPLE_RATE * note_s)))
    return samples.tobytes()


def _wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(SAMPLE_RATE)
        clip.writeframes(pcm)
    return buffer.getvalue()
