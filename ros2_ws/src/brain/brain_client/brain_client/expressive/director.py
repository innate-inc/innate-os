# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""How the body performs each sentence the robot speaks. Pure: no ROS, no I/O.

The planner performs the words themselves (``prompt.speech_prompt``) and decides how big each
performance is; an emote tag in the sentence overrides it with the tag's prompt. The director adds
only what must be instant or must work without the planner server: a stand-in preset when the
words name a feeling outright, and the fallback while the server is down — that preset, or a small
neutral beat paced by estimated speech time, so a long reply is not a fidget per sentence.
"""

from __future__ import annotations

import functools
import math
import random
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from brain_client.expressive import presets
from brain_client.expressive.motion import Clip
from brain_client.expressive.prompt import speech_prompt

if TYPE_CHECKING:
    from collections.abc import Sequence

# Fitted on 232 sim TTS clips (Cartesia sonic-3.5 at speed 1.0); the robot's speaker runs ~10 % faster.
SPEECH_BASE_S = 0.46
SPEECH_PER_CHAR_S = 0.055
BEAT_AFTER_S = 4.0

# Words that name a feeling outright, matched as whole words or phrases; the earliest in the sentence
# wins. Narrower than the presets' keywords, which read a prompt: in speech "down the hall" is not sad.
SPOKEN_CUES: dict[str, tuple[str, ...]] = {
    "happy": ("congratulations", "congrats", "yay", "hooray", "haha", "so happy", "great news", "good news", "glad"),
    "sad": ("so sorry", "sorry to hear", "condolences", "sadly", "that's sad", "heartbreaking", "i miss"),
    "surprised": ("wow", "whoa", "oh my", "no way", "what was that"),
    "excited": ("so excited", "can't wait", "awesome", "amazing", "thrilled", "let's go"),
    "scared": ("eek", "yikes", "scary", "scared", "afraid"),
    "angry": ("angry", "furious", "how dare", "outrageous", "unacceptable"),
    "relieved": ("phew", "thank goodness", "what a relief", "relieved"),
    "affectionate": (
        "hello",
        "hi there",
        "hey there",
        "welcome back",
        "welcome home",
        "you're welcome",
        "thank you",
        "love you",
        "aww",
        "goodbye",
        "bye",
        "see you",
        "goodnight",
        "good night",
    ),
    "confused": ("confused", "i don't understand", "not sure i understand", "say that again", "pardon", "huh"),
    "thinking": ("hmm", "let me think", "let me see", "good question"),
    "agreeing": ("of course", "absolutely", "sure thing", "you got it", "will do", "sounds good", "you're right"),
    "disagreeing": ("i don't think so", "no thanks", "no thank you", "not a good idea", "i disagree"),
    "sleepy": ("tired", "sleepy", "yawn"),
    "proud": ("i did it", "nailed it", "proud"),
    "curious": ("interesting", "i wonder", "tell me more", "fascinating", "curious"),
    "bored": ("boring", "bored"),
}

# Small, low-energy recipes for a plain sentence while the planner server is down. Posture within about ±.3
# reads as frozen on this body, so the values carry the speech planner's visibility gain (expressive/ml/speech.py).
BEATS: dict[str, str] = {
    "nod": "go .35 p=.47 a=.17 E=.6 | go .25 p=.09 | go .25 p=.4 | go .5 p=.17 a=0 E=.5 | hold .6",
    "lean-in": "go .6 a=.47 p=.4 k=.17 E=.7 | hold 1 E=.5 | go .8 a=.09 p=.17 k=0",
    "tilt": "go .5 k=.54 p=.33 E=.6 | hold .9 | go .6 k=0 p=.09",
    "open-hand": "go .5 x=.47 z=.25 a=.17 g=.4 E=.8 | hold .6 | go .7 x=0 z=0 a=0 g=.15 E=.5",
    "bob": "go .3 z=.17 p=.25 E=.7 | osc 1.2 z .14 .6 | go .5 z=0 p=.09",
    "settle": "go .7 z=-.21 a=-.14 p=-.09 E=.4 | hold .8 | go .8 z=.09 p=.21 E=.5",
    "lift": "go .3 z=.33 x=.17 p=.33 g=.25 E=.9 | hold .3 | go .6 z=0 x=0 p=.09 g=.15 E=.5",
    "sway": "go .4 k=-.33 p=.25 E=.6 | go .6 k=.33 | go .5 k=0 p=.09 | hold .4",
}

_WORDS = re.compile(r"[^a-z']+")


@dataclass(frozen=True)
class Direction:
    prompt: str  # what the planner performs
    tagged: bool  # an emote tag's prompt: it keeps the whole clip chain, the brain's LLM included
    stand_in: str | None  # a preset to play the moment the sentence is heard, until its clip lands
    fallback: str | None  # a preset or beat name for when the planner server cannot answer; None keeps still


class Director:
    """One per driver, fed the sentences in the order they are spoken."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)
        self._still_s = math.inf  # estimated speech since the fallback last moved the body
        self._last_beat: str | None = None

    def direct(
        self, sentence: str, tags: Sequence[str] = (), before: str | None = None, heard: str | None = None
    ) -> Direction:
        """``before`` is the previous sentence of the same reply (None opens a reply), ``heard`` what the
        person last said, used only by a reply's first sentence as the speech planners were trained; an
        emote tag's prompt replaces the sentence's own."""
        if before is None:
            self._still_s = math.inf  # the body idled while the person spoke
        if tags:
            self._still_s = speech_seconds(sentence)
            return Direction(tags[-1], tagged=True, stand_in=presets.match(tags[-1]), fallback=None)
        stand_in = spoken_preset(sentence)
        spoken = speech_seconds(sentence)
        fallback = stand_in or self._beat(spoken)
        self._still_s = spoken + (0.0 if fallback else self._still_s)
        prompt = speech_prompt(sentence, heard=None if before else heard, before=before)
        return Direction(prompt, False, stand_in, fallback)

    def reset(self) -> None:
        self._still_s = math.inf

    def _beat(self, spoken: float) -> str | None:
        """A beat when, by the middle of this sentence, the body would have been still for ``BEAT_AFTER_S``."""
        if self._still_s + spoken / 2 < BEAT_AFTER_S:
            return None
        self._last_beat = self._rng.choice([name for name in BEATS if name != self._last_beat])
        return self._last_beat


def speech_seconds(sentence: str) -> float:
    return SPEECH_BASE_S + SPEECH_PER_CHAR_S * len(sentence) if sentence else 0.0


def spoken_preset(sentence: str) -> str | None:
    """The preset whose spoken cue comes first in ``sentence``, None when none appears."""
    text = " " + _WORDS.sub(" ", sentence.lower().replace("’", "'")) + " "
    found = [(at, name) for name, cues in SPOKEN_CUES.items() for cue in cues if (at := text.find(f" {cue} ")) >= 0]
    return min(found)[1] if found else None


def fallback_clip(name: str, seed: int = 0, prompt: str = "") -> Clip:
    """The clip of a preset or a beat name, relabelled with ``prompt`` for display."""
    if name in presets.PRESETS:
        return presets.clip(name, seed, prompt)
    built = _beat_clip(name, seed)
    return replace(built, prompt=prompt) if prompt else built


@functools.lru_cache(maxsize=32)
def _beat_clip(name: str, seed: int) -> Clip:
    return Clip.from_recipe(BEATS[name], name=f"beat-{name}", seed=seed)
