# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Built-in recipes: instant gestures with no planner, and the fallback that keeps expression from
ever going silent.

Each preset is written for the basis's body vocabulary (rise +1 a mast, -1 the arm hanging toward the
floor; approach +1 a reach toward the person, -1 the arm drawn up beside the head; expand +1 the arm
out to the side with the claw open), with a shape AND a timing of its own so no two read alike.
``match`` scores keywords against the prompt's words: a keyword of 4+ letters matches any word it
starts (``frighten`` -> frightened), a shorter one only the whole word, a phrase only as a phrase.
"""

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
            "Throw the arm up and out with the claw open, bounce and sway side to side like a wave, settle bright.",
            "go .35 x=.9 z=.5 p=.6 g=.8 E=2 | osc 1.8 b 14 .6 E=3 | osc 1.5 z .25 .5 E=3"
            " | go .5 x=.6 z=.3 p=.4 g=.5 E=1.5 | hold .5 E=1",
            (
                "happ",
                "joy",
                "joyf",
                "glad",
                "cheer",
                "delight",
                "yay",
                "smil",
                "great",
                "wonderful",
                "fun",
                "laugh",
                "celebrat",
                "song",
                "music",
                "danc",
                "play",
            ),
        ),
        Preset(
            "sad",
            "sad. You just heard disappointing news.",
            "Sink at once: the arm sags and the claw hangs, the head drops to the floor, then turn a little away "
            "and stay there.",
            "go 1 z=-1 p=-1 g=0 E=.2 | hold 1 E=.1 | go 1.5 b=-20 d=-.06 E=.1 | hold 3 E=.05",
            (
                "sad",
                "unhapp",
                "down",
                "disappoint",
                "deflat",
                "sorry",
                "gloom",
                "lonel",
                "heartbr",
                "miss",
                "cry",
                "tear",
                "grie",
                "depress",
                "upset",
                "blue",
                "mourn",
                "embarrass",
                "ashamed",
                "guilt",
            ),
        ),
        Preset(
            "curious",
            "curious. Something new caught your eye.",
            "Reach toward it with the head up, cock the claw one way then the other, inch closer.",
            "go .6 a=.7 p=.7 k=.7 g=.35 E=1 | hold .7 E=.5 | go .4 k=-.7 E=1.2 | hold .7 E=.5"
            " | go .5 a=.9 k=.3 d=.08 | hold .6 | go .8 a=.3 k=0 d=0 p=.4 E=.6",
            (
                "curio",
                "interest",
                "wonder",
                "intrigu",
                "hmm",
                "new",
                "look",
                "investigat",
                "sniff",
                "explor",
                "notic",
                "inspect",
                "peek",
                "smell",
                "hunt",
                "stalk",
                "what's that",
            ),
        ),
        Preset(
            "excited",
            "excited. You can hardly wait.",
            "Snap the arm up with the claw wide open, bounce fast, wiggle the whole body, chatter, then simmer.",
            "go .25 z=.8 x=.5 p=.8 g=.9 E=5 | osc 1.6 z .25 .4 E=6 | osc 1.2 b 15 .5 E=6 | osc 1 g .35 .35"
            " | go .5 z=.4 x=.3 g=.5 E=2",
            (
                "excit",
                "thrill",
                "can't wait",
                "wow",
                "awesome",
                "party",
                "hooray",
                "eager",
                "pump",
                "ecstat",
                "bounc",
                "jump",
                "woohoo",
                "lottery",
                "dog",
            ),
        ),
        Preset(
            "proud",
            "proud. You finally solved it.",
            "Rise slowly to a tall open stance with the head held high and the chest pushed toward the person, th"
            "en hold still.",
            "go 1.5 z=.7 x=.5 p=1 d=.06 g=.2 E=.3 | hold 2 E=.15 | go 1 b=12 E=.2 | hold 1 | go 1 b=0 E=.2 | hold .6",
            (
                "proud",
                "pride",
                "solved",
                "did it",
                "confiden",
                "accomplish",
                "nailed",
                "success",
                "triumph",
                "victor",
                "win",
                "champion",
                "brag",
                "smug",
                "applau",
                "soldier",
            ),
        ),
        Preset(
            "confused",
            "confused. That makes no sense.",
            "Head up, look one way and then the other with the claw tipping each way, a puzzled pause, look again.",
            "go .5 p=.5 a=.3 b=-20 k=.7 g=.3 E=.8 | hold .7 E=.4 | go .5 b=20 k=-.7 | hold .7 | go .4 b=-10 k=.5 "
            "g=.5 | hold .5 | go .6 b=0 k=0 p=.3 a=.1 g=.2 E=.5 | hold .6",
            (
                "confus",
                "puzzl",
                "huh",
                "don't understand",
                "lost",
                "strange",
                "weird",
                "baffl",
                "perplex",
                "unsure",
                "doubt",
                "bewilder",
                "dizz",
                "drunk",
                "tipsy",
            ),
        ),
        Preset(
            "surprised",
            "surprised. That came out of nowhere.",
            "A beat of stillness, then the body jerks back, the arm snaps in, head flies up and the claw gapes; f"
            "reeze, slowly recover.",
            "hold .4 | go .15 a=-1 p=1 g=1 d=-.1 E=8 | hold 1.2 E=1 | go 1 a=-.3 p=.4 g=.4 d=-.08 E=1 | hold .6",
            (
                "surpris",
                "whoa",
                "shock",
                "startl",
                "unexpected",
                "gasp",
                "astonish",
                "amaz",
                "wait what",
                "caught",
                "sneez",
            ),
        ),
        Preset(
            "scared",
            "scared. Something big is coming at you.",
            "Shrink and get away: the arm pulls in, the head drops, the body backs off and turns aside, trembling.",
            "go .3 a=-1 x=-1 z=-.8 p=-1 g=0 d=-.15 b=-30 E=7 | go .8 d=-.25 b=-45 E=8 | hold 2.5 E=8 | go 1 p=-.7 E=4",
            (
                "scar",
                "afraid",
                "fear",
                "frighten",
                "terrif",
                "nervous",
                "anxi",
                "danger",
                "panic",
                "flinch",
                "cower",
                "spider",
                "monster",
                "hid",
                "shy",
                "stage",
            ),
        ),
        Preset(
            "angry",
            "angry. You have had enough.",
            "Square up with the head lowered in a glare, then lunge at the person with the claw snapping, twice, "
            "and hold the glare.",
            "go .4 a=.6 p=-.8 x=-.2 g=.9 E=3 | go .2 d=.12 a=.9 g=0 E=9 | go .3 d=.02 a=.6 g=.9 E=4 | go .2 d=.15"
            " a=.9 g=0 E=9 | go .4 d=.06 a=.6 g=.6 E=4 | hold 1 E=3 | go .8 a=.3 p=-.4 g=.2 E=2",
            (
                "angr",
                "mad",
                "furious",
                "annoy",
                "enough",
                "frustrat",
                "rage",
                "grr",
                "irritat",
                "hate",
                "grump",
                "fury",
                "livid",
                "disgust",
                "gross",
                "yuck",
                "jealous",
                "snake",
                "shoo",
            ),
        ),
        Preset(
            "sleepy",
            "sleepy. You keep nodding off.",
            "Sink heavier and heavier until the head hangs, jerk half awake, then sink for good, turned a little away.",
            "go 2 z=-.8 p=-1 g=0 b=-15 E=.1 | go .4 z=-.3 p=-.3 E=.8 | go 2 z=-1 p=-1 b=-25 E=0 | hold 2.5 E=0",
            ("sleep", "tired", "yawn", "exhaust", "drows", "bed", "night", "nap", "doz", "snooz", "weary"),
        ),
        Preset(
            "agreeing",
            "nodding yes. You agree.",
            "Lean in and nod with head and arm together, in clear beats, then settle attentive.",
            "go .3 p=.5 a=.4 g=.25 E=1 | go .25 p=-.2 a=.55 z=-.15 | go .25 p=.5 a=.4 z=0 | go .25 p=-.2 a=.55 z=-.15"
            " | go .25 p=.5 a=.4 z=0 | go .25 p=-.2 a=.55 z=-.15 | go .4 p=.3 a=.3 z=0 E=.6 | hold .4",
            (
                "yes",
                "agree",
                "nod",
                "pleased",
                "okay",
                "ok",
                "sure",
                "right",
                "exactly",
                "understood",
                "got it",
                "correct",
                "indeed",
                "absolutely",
            ),
        ),
        Preset(
            "disagreeing",
            "shaking your head no. You refuse.",
            "Pull the arm in, then turn the whole body firmly side to side, three times, and stop square.",
            "go .3 p=0 x=-.8 a=-.3 E=1 | go .3 b=25 p=-.2 | go .4 b=-25 | go .4 b=25 | go .4 b=-25 | go .4 b=25 |"
            " go .4 b=-25 | go .5 b=0 x=0 a=0 E=.5 | hold .4",
            ("no", "disagree", "refus", "nope", "shake", "never", "wrong", "don't", "deny", "reject", "nah", "not"),
        ),
        Preset(
            "thinking",
            "thinking. Let me figure this out.",
            "Bring the claw up beside the head and look up and away, roll it slowly while pondering, then come back.",
            "go .9 a=-.7 p=.5 k=.4 g=.1 E=.5 | hold 1.2 E=.3 | osc 2.4 k .2 1.2 E=.4 | go .6 b=-10 p=.6 | hold 1 E=.3"
            " | go .9 a=0 k=0 b=0 p=0 E=.5",
            (
                "think",
                "consider",
                "ponder",
                "figure",
                "let me see",
                "plan",
                "calculat",
                "reflect",
                "decid",
                "idea",
                "rememb",
                "wondering",
            ),
        ),
        Preset(
            "affectionate",
            "affectionate. You are happy to see a friend.",
            "Rise softly toward the friend with the head up and the claw half open, sway the tilted claw, lean closer.",
            "go 1.2 a=.5 z=.5 p=1 g=.4 d=.15 E=.3 | osc 3 k .35 1.5 E=.3 | go 1 a=.7 z=.2 d=.2 | hold 1.2 E=.2",
            (
                "affection",
                "love",
                "friend",
                "hug",
                "cute",
                "sweet",
                "miss you",
                "welcome",
                "hello",
                "hi",
                "adore",
                "cuddl",
                "fond",
                "thank",
                "grateful",
                "greet",
            ),
        ),
        Preset(
            "bored",
            "bored. Nothing is happening.",
            "Sag with the head down, chew on nothing for a while, then turn away and stay slumped.",
            "go 2 z=-.5 a=-.3 p=-.5 E=.1 | osc 4 g .2 2 E=.1 | hold 2 E=0 | go 1.5 b=-25 | hold 2 E=0",
            ("bore", "meh", "whatever", "wait", "dull", "nothing", "tedious", "sigh", "impatien", "idle"),
        ),
        Preset(
            "relieved",
            "relieved. Phew, it worked out.",
            "Tense and tucked for a moment, then a long exhale: everything loosens, sinks a little and settles calm.",
            "go .6 a=-.4 p=.5 g=.1 E=1.5 | hold .5 E=1 | go 2 a=0 p=0 z=-.3 g=.2 E=.1 | hold 1.5 E=0 | go 1 z=0 p"
            "=.2 E=.1 | hold 1 E=0",
            ("relie", "phew", "finally", "safe", "calm", "worked", "relax", "ease", "breath"),
        ),
        Preset(
            "listening",
            "listening. Someone is talking to you.",
            "Turn the attention up to the speaker: a slight lean in, head raised, a small acknowledging nod.",
            "go .8 a=.25 p=.6 k=.15 E=.6 | hold 1.5 E=.4 | go .4 p=.4 | go .4 p=.65 | hold 1 E=.4"
            " | go .8 a=.1 p=.4 k=0 E=.5",
            ("listen", "attent", "hear", "tell me", "go on", "i see", "pay attention", "focus"),
        ),
    )
}
DEFAULT = "listening"
_WORDS = re.compile(r"[^a-z']+")


def _hits(keyword: str, words: list[str], text: str) -> bool:
    if " " in keyword:
        return f" {keyword} " in text
    if len(keyword) >= 4:
        return any(word.startswith(keyword) for word in words)
    return keyword in words


def match(prompt: str) -> str:
    """The preset whose keywords best match ``prompt`` (``DEFAULT`` when nothing does; ties go to the first)."""
    text = " " + _WORDS.sub(" ", prompt.lower()) + " "
    words = text.split()
    scores = {name: sum(_hits(k, words, text) for k in p.keywords) for name, p in PRESETS.items()}
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
