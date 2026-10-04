# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Built-in recipes: instant gestures with no planner, and the fallback that keeps expression from
ever going silent."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, replace

from brain_client.expressive.motion import Clip


@dataclass(frozen=True)
class Preset:
    name: str
    prompt: str
    idea: str
    recipe: str
    keywords: tuple[str, ...]


PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset(
            "happy",
            "happy. Something lovely just happened.",
            "Rise tall and open, bounce on the beat, sway the cocked gripper, settle bright.",
            "go .4 z=.5 x=.4 p=.6 g=.6 a=.2 E=3 | osc 2 z .2 .5 E=4 | osc 1.5 k .3 .75 E=3"
            " | go .6 z=.3 x=.2 p=.4 g=.4 k=0 E=1.5 | hold .6 E=1",
            ("happy", "joy", "glad", "cheerful", "delighted", "yay", "smile", "great"),
        ),
        Preset(
            "sad",
            "sad. You just heard disappointing news.",
            "Sink slowly: the arm droops and folds, gaze to the floor, drifting back and away.",
            "go 1.8 z=-.7 x=-.5 p=-.8 a=-.2 g=.05 d=-.05 b=-8 E=.4 | hold 2 E=.2 | osc 2.5 k .12 2.5 E=.3 | hold 1",
            ("sad", "unhappy", "down", "disappointed", "deflated", "sorry", "gloomy", "lonely", "heartbroken", "miss"),
        ),
        Preset(
            "curious",
            "curious. Something new caught your eye.",
            "Lean in and look up, cock the gripper one way then the other, edge closer.",
            "go .5 p=.6 a=.5 k=.6 z=.2 g=.3 d=.06 E=1.5 | hold .8 E=.6 | go .4 k=-.6 E=1.8 | hold .8 E=.6"
            " | go .4 k=.3 a=.7 d=.1 E=1.2 | hold .7 | go .8 a=.2 k=0 d=0 p=.3 E=.8",
            ("curious", "interested", "what", "wonder", "intrigued", "hmm", "new", "look"),
        ),
        Preset(
            "excited",
            "excited. You can hardly wait.",
            "Snap tall and wide with the mouth open, bounce, wiggle the base, chatter, then simmer.",
            "go .25 z=.8 x=.7 p=.7 g=.8 E=6 | osc 1.6 z .25 .45 E=7 | osc 1.2 b 12 .6 E=7 | go .3 a=.5 d=.08 E=5"
            " | osc 1 g .3 .4 | go .6 z=.4 x=.3 a=0 d=0 g=.4 E=2",
            ("excited", "thrilled", "can't wait", "wow", "awesome", "party", "hooray"),
        ),
        Preset(
            "proud",
            "proud. You finally solved it.",
            "Rise to a mast and open up, chin up, a slow satisfied turn, then hold the pose.",
            "go 1 z=.9 x=.5 p=.6 a=-.1 g=.3 E=1 | hold 1.5 E=.5 | osc 1.6 b 8 1.6 E=.7 | go .8 z=.7 p=.4 E=.6 | hold .8",
            ("proud", "solved", "did it", "confident", "accomplished", "nailed", "success"),
        ),
        Preset(
            "confused",
            "confused. That makes no sense.",
            "Hold the gripper up and tilt it one way, then the other with a small turn, half-fold, give up and reset.",
            "go .6 k=.7 p=.3 a=.35 z=.3 g=.2 E=1 | hold .9 E=.5 | go .5 k=-.6 p=.1 b=-10 E=1.2 | hold .9"
            " | go .5 k=.4 b=8 z=.15 x=-.2 E=1 | hold .6 | go .7 k=0 b=0 a=0 z=0 x=0 p=0 E=.8",
            ("confused", "puzzled", "huh", "don't understand", "lost", "strange", "weird"),
        ),
        Preset(
            "surprised",
            "surprised. That came out of nowhere.",
            "A beat of stillness, then a snap up and back with the mouth wide open, freeze, recover.",
            "go .5 E=.5 | hold .3 | go .15 z=.8 a=-.6 x=.5 p=.8 g=.9 d=-.06 E=9 | hold 1.2 E=1"
            " | go .8 z=.3 a=0 x=.1 p=.3 g=.4 d=-.04 E=1.5 | hold .6",
            ("surprised", "surprise", "whoa", "oh", "shocked", "startled", "unexpected", "gasp"),
        ),
        Preset(
            "scared",
            "scared. Something big is coming at you.",
            "Recoil and shrink, back away and turn aside, trembling, eyes still on the threat.",
            "go .2 a=-.8 z=-.3 x=-.6 p=.4 g=.05 d=-.12 E=8 | hold .6 E=6 | go .8 z=-.6 x=-.8 d=-.18 b=-15 E=5"
            " | hold 1.5 E=6 | go 1 a=-.4 z=-.4 x=-.5 E=3",
            ("scared", "scary", "afraid", "fear", "frightened", "terrified", "nervous", "anxious", "danger"),
        ),
        Preset(
            "angry",
            "angry. You have had enough.",
            "Square up head down, then two biting lunges forward with the gripper, glare, ease off.",
            "go .4 z=.4 a=.6 x=.3 p=-.2 g=0 E=3 | go .15 a=.9 d=.08 g=.9 E=9 | go .3 a=.6 g=.1 E=6"
            " | go .15 a=.9 d=.12 g=.9 E=9 | go .4 a=.5 d=.05 g=.05 E=4 | hold .8 E=3 | go .8 a=.2 z=.2 x=0 d=0 E=1.5",
            ("angry", "mad", "furious", "annoyed", "enough", "frustrated", "rage", "grr"),
        ),
        Preset(
            "sleepy",
            "sleepy. You keep nodding off.",
            "Droop heavier and heavier, jolt awake, then sink again and stay down.",
            "go 1.5 z=-.4 p=-.5 x=-.3 g=.1 E=.4 | go 1.2 z=-.8 p=-.9 E=.2 | go .3 z=-.2 p=-.1 E=2 | hold .5 E=.6"
            " | go 1.8 z=-.85 p=-1 a=-.1 g=.05 E=.2 | hold 1.5 E=.1",
            ("sleepy", "tired", "sleep", "yawn", "exhausted", "drowsy", "bed", "night"),
        ),
        Preset(
            "agreeing",
            "nodding yes. You agree.",
            "Lean in a little and nod in clear beats, then settle attentive.",
            "go .3 p=.4 a=.3 g=.25 E=1.5 | osc 2.4 p .35 .6 E=1.5 | go .5 p=.2 a=.1 E=.8 | hold .5",
            ("yes", "agree", "nod", "pleased", "okay", "ok", "sure", "right", "exactly", "understood", "got it"),
        ),
        Preset(
            "disagreeing",
            "shaking your head no. You refuse.",
            "Pull back slightly and shake the whole body side to side, then stop square.",
            "go .3 p=.2 a=-.2 x=-.2 E=1.5 | osc 2.4 b 14 .6 E=2 | go .5 b=0 a=0 x=0 E=.8 | hold .4",
            ("no", "disagree", "refuse", "nope", "shake", "never", "wrong", "don't"),
        ),
        Preset(
            "thinking",
            "thinking. Let me figure this out.",
            "Raise the gripper toward the head and look up, a slow tilt, a small turn away while pondering, back.",
            "go .8 p=.5 k=.4 z=.45 a=.1 g=.1 E=.6 | hold 1.2 E=.4 | osc 2 k .15 1.4 E=.5 | go .6 p=.3 k=.6 b=-10"
            " | hold 1 E=.3 | go .8 p=0 k=0 b=0 a=0 z=0 E=.6",
            ("thinking", "think", "consider", "ponder", "figure", "let me see", "wondering", "plan"),
        ),
        Preset(
            "affectionate",
            "affectionate. You are happy to see a friend.",
            "Lean in close and look up softly, sway the tilted gripper gently, stay near.",
            "go 1 a=.7 p=.5 k=.4 x=.2 g=.35 d=.08 E=.8 | osc 2.4 k .25 1.2 E=.8 | hold .8 E=.5 | go 1 a=.4 k=.1 d=.04 E=.6",
            ("affectionate", "love", "friend", "hug", "cute", "sweet", "miss you", "welcome", "hello", "hi"),
        ),
        Preset(
            "bored",
            "bored. Nothing is happening.",
            "Sag, look away one way and the other, chew on nothing, sag again.",
            "go 1.2 z=-.3 p=-.3 x=-.2 E=.4 | hold 1 E=.2 | go .8 b=-20 p=.1 | hold .8 | go .8 b=12"
            " | osc 2 g .15 1 E=.3 | go 1 b=0 p=-.3 E=.2 | hold .8",
            ("bored", "boring", "meh", "whatever", "waiting", "dull", "nothing"),
        ),
        Preset(
            "relieved",
            "relieved. Phew, it worked out.",
            "Draw up as if inhaling, then let it all out in a long slump, and come back to easy.",
            "go .8 z=.4 p=.5 x=.3 g=.5 E=1.5 | go 1.2 z=-.3 p=-.3 x=-.2 g=.2 a=-.1 E=.6 | hold .8 E=.4"
            " | go 1 z=0 p=.1 x=0 a=0 g=.15 E=.6",
            ("relieved", "relief", "phew", "finally", "safe", "calm", "worked"),
        ),
    )
}
DEFAULT = "curious"
_WORDS = re.compile(r"[^a-z']+")


def match(prompt: str) -> str:
    """The preset whose keywords best match ``prompt`` (``DEFAULT`` when nothing does)."""
    text = " " + _WORDS.sub(" ", prompt.lower()) + " "
    scores = {name: sum(f" {k} " in text for k in p.keywords) for name, p in PRESETS.items()}
    best = max(scores, key=lambda name: scores[name])
    return best if scores[best] else DEFAULT


@functools.lru_cache(maxsize=64)
def _clip(name: str, seed: int) -> Clip:
    preset = PRESETS[name]
    return Clip.from_recipe(preset.recipe, name=name, seed=seed, prompt=preset.prompt, idea=preset.idea)


def clip(name: str, seed: int = 0, prompt: str = "") -> Clip:
    """The procedural clip of preset ``name`` (cached per seed); ``prompt`` relabels it for display."""
    built = _clip(name, seed)
    return replace(built, prompt=prompt) if prompt else built
