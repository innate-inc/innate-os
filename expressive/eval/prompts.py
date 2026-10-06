"""What the eval asks for: the judges' fixed label vocabulary, the presets' target labels, and 30 held-out
prompts that no preset covers (feelings, reactions, animals, characters, multi-phase stories, and siblings
of the physical probes)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import _core  # noqa: F401

from brain_client.expressive import presets

JUDGE_JS = Path(__file__).resolve().parents[2] / "webapp" / "js" / "expression" / "judge.js"
Group = Literal["preset", "ood"]
Source = Literal["preset", "llm"]


def _judge_labels() -> tuple[str, ...]:
    """The studio judge's vocabulary, read from judge.js so the two judges can never drift apart."""
    found = re.search(r"export const JUDGE_LABELS = \[(.*?)\];", JUDGE_JS.read_text(), re.S)
    if found is None:
        raise RuntimeError(f"JUDGE_LABELS not found in {JUDGE_JS}")
    return tuple(re.findall(r'"([a-z]+)"', found.group(1)))


LABELS = _judge_labels()

# Acts with no feeling label (nodding, shaking no, pondering) score on the free-text description only.
PRESET_LABELS: dict[str, tuple[str, ...]] = {
    "happy": ("happy",),
    "sad": ("sad",),
    "curious": ("curious",),
    "excited": ("excited",),
    "proud": ("proud",),
    "confused": ("confused",),
    "surprised": ("startled",),
    "scared": ("scared",),
    "angry": ("angry",),
    "sleepy": ("sleepy",),
    "agreeing": (),
    "disagreeing": (),
    "thinking": (),
    "affectionate": ("affectionate",),
    "bored": ("bored",),
    "relieved": ("calm",),
    "listening": ("calm", "curious"),
}

OOD: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("embarrassed", "embarrassed. Everyone just saw you trip over your own feet.", ("ashamed",)),
    ("disgusted", "disgusted. Someone holds a plate of rotten fish under your nose.", ("disgusted",)),
    ("guilty", "guilty. You broke the vase and the owner just walked in.", ("ashamed", "scared")),
    ("stage-fright", "nervous. You are about to walk on stage in front of a huge crowd.", ("scared",)),
    ("jealous", "jealous. Your friend is getting all the attention instead of you.", ("sad", "angry")),
    ("grateful", "grateful. A stranger just helped you up after you fell.", ("affectionate", "happy")),
    ("smug", "smug. You knew you were right all along and everyone can see it now.", ("proud",)),
    ("lonely", "lonely. Everyone else left the party without you.", ("sad",)),
    ("spider", "a spider just dropped onto your arm.", ("scared", "startled", "disgusted")),
    ("name-called", "someone calls your name from behind you.", ("curious", "startled")),
    ("favourite-song", "your favourite song comes on and you can't help moving to it.", ("happy", "playful")),
    ("cookies", "you smell fresh cookies coming out of the oven.", ("curious", "happy", "excited")),
    ("dog-greeting", "a dog greeting its owner at the door after a long day.", ("excited", "happy", "affectionate")),
    ("turtle", "an old turtle, slow and deliberate, looking around.", ("calm", "sleepy")),
    ("pecking-bird", "a little bird hopping and pecking at seeds on the ground.", ("curious", "playful")),
    ("snake", "a snake rearing up, ready to strike.", ("angry",)),
    ("tail-chase", "a puppy chasing its own tail.", ("playful",)),
    ("grumpy-neighbour", "a grumpy old man shooing kids off his lawn.", ("angry",)),
    ("soldier", "a proud soldier standing at attention for inspection.", ("proud",)),
    ("thief", "a sneaky thief tiptoeing past a sleeping guard.", ()),
    ("shy-child", "a shy child hiding from a stranger.", ("scared", "ashamed")),
    ("rock-star", "a rock star soaking up the applause after the encore.", ("proud", "excited")),
    ("tickle-sneeze", "your nose tickles more and more until it bursts out in a huge sneeze.", ()),
    ("cat-mouse", "a cat hunting a mouse: crouch, wait without moving, then pounce.", ()),
    ("tipsy", "tipsy after one drink too many. The room is spinning.", ("confused", "sleepy")),
    ("lottery", "you just won the lottery! You jump for joy.", ("excited", "happy")),
    ("crossing", "about to cross a busy street. Look left, look right, then go.", ("curious",)),
    ("cookie-jar", "you reach for a cookie, get caught, then pretend you were doing nothing.", ("ashamed", "playful")),
    ("waiting", "you wait patiently, get more and more bored, and finally doze off.", ("bored", "sleepy")),
    (
        "false-alarm",
        "you hear a noise, freeze, creep over to investigate, then relax when it is nothing.",
        ("curious", "startled", "calm"),
    ),
)


@dataclass(frozen=True)
class Item:
    """One prompt to plan, play and judge; ``expects`` empty = no label fits, judged on the description."""

    id: str
    group: Group
    source: Source
    prompt: str
    expects: tuple[str, ...]
    recipe: str | None = None


def items() -> list[Item]:
    """Each preset twice (its hand-written recipe and a planner-written one), then the held-out prompts."""
    hand = [
        Item(f"preset-{name}", "preset", "preset", p.prompt, PRESET_LABELS[name], p.recipe)
        for name, p in presets.PRESETS.items()
    ]
    planned = [
        Item(f"llm-{name}", "preset", "llm", p.prompt, PRESET_LABELS[name]) for name, p in presets.PRESETS.items()
    ]
    held_out = [Item(f"ood-{slug}", "ood", "llm", prompt, expects) for slug, prompt, expects in OOD]
    assert {label for item in hand + held_out for label in item.expects} <= set(LABELS)
    return hand + planned + held_out
