"""Teacher data for the speech planner: MARS's spoken replies in context, performed sentence by sentence.

  python -m ml.speech replies --work W     # Codex writes ~3k exchanges: what the person said, MARS's spoken reply
  python -m ml.speech teach   --work W     # the teacher performs every sentence (frozen prompt + a size policy)
  python -m ml.speech build   --work W     # -> distill_data/{speech,speech_val}.jsonl + speech_report.json

A reply is split into sentences exactly as the robot's speech streamer splits it, and each sentence becomes
``speech_prompt(sentence, heard, before)``: ``heard`` (the person's last words) on a reply's first sentence, ``before``
(the previous sentence) on the rest. Whole replies are held out, so no held-out sentence's context ever trains.
The size policy lives only in the teacher's instructions (``TEACH_PROMPT``): the student learns it from the answers.
``build`` then applies ``visible`` to every answer: the teacher sizes beats right relative to peaks, but its plain-line
beats moved the gripper ~7 cm, which whole-reply videos read as frozen. Every call is cached under W like
``ml.author``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .author import _strict, codex, system_prompt

DATA = Path(__file__).with_name("distill_data")
SIZES = ("beat", "continue", "peak")
CUES = ("none", "yes", "no", "hello", "bye", "look", "come", "think", "laugh")
WORDS_PER_S = 2.7  # TTS speaking rate the clips are timed against
MAX_IDEA_WORDS = 14
VAL_PER_CATEGORY = 4  # held-out eval replies per scene
DEV_PER_CATEGORY = 1  # replies per scene for the SFT val loss (checkpoint choice), never trained or evaluated on
BOTH_CONTEXTS_PERCENT = 10  # later sentences that also carry ``heard``, in case a caller passes it every time
# ``visible``: v -> sign(v) (1 - (1 - |v| / full)^GAIN) full on the stance and posture channels, full = channel range
GAIN = 1.8
GAIN_FULL = {"a": 1.0, "x": 1.0, "z": 1.0, "p": 1.0, "k": 1.0, "b": 60.0, "d": 0.25}


@dataclass(frozen=True)
class Scene:
    slug: str
    brief: str
    spontaneous: int = 3  # of 30 exchanges, how many MARS opens itself (heard = "")


SCENES: list[Scene] = [
    Scene(
        "smalltalk", "small talk: how are you, the weather, weekend plans, favourite things, MARS's own day, chit-chat"
    ),
    Scene("facts", "answering general-knowledge questions, definitions, trivia, quick maths, homework help"),
    Scene(
        "explain", "explaining how something works over several sentences: a recipe step, a science idea, its sensors"
    ),
    Scene(
        "skills",
        "narrating what it is about to do or is doing with its arm: picking up a sock or a cup, putting toys in a box, "
        "handing an object over, waving, tidying",
        12,
    ),
    Scene(
        "navigation",
        "driving around the home: going to a room, arriving, 'follow me', something blocking the way, finding the "
        "charger, getting lost, mapping a new room",
        12,
    ),
    Scene(
        "memory", "finding lost things from memory ('I saw your keys on the counter this morning'), recalling people"
    ),
    Scene("success", "a task finished: done, nailed it, proud of the result, the person thanks it"),
    Scene(
        "failure", "a task failed: the object slipped, could not reach it, got stuck, an error; apologising, retrying"
    ),
    Scene("jokes", "telling jokes and puns with a punchline, playful teasing, comebacks, banter"),
    Scene("laughter", "laughing at the person's joke or a funny moment: haha, giggling, cracking up, then a remark"),
    Scene("comfort", "the person is sad, stressed, sick, lonely or had a bad day: gentle comfort and reassurance"),
    Scene("greetings", "hello, good morning, welcome home, meeting someone new, a child, a guest at the door", 8),
    Scene("farewells", "goodbye, good night, see you later, someone leaving for work or a trip"),
    Scene("agree", "saying yes: agreeing, confirming, accepting a request, 'sure, on it', 'exactly'"),
    Scene("refuse", "saying no: refusing an unsafe or impossible request, politely disagreeing, correcting a mistake"),
    Scene("thinking", "thinking out loud: 'hmm, let me think', not sure, weighing options, trying to remember"),
    Scene(
        "attention",
        "directing attention: 'look over there', 'it's on your left', 'behind you', 'that one', 'come here', "
        "'let me see', 'show me', 'turn around'",
    ),
    Scene("excitement", "good news, birthdays, surprises, a win, a gift, a celebration"),
    Scene(
        "negative",
        "strong negative moments: fear of a loud bang or a spider, frustration, annoyance, embarrassment, "
        "disappointment, being scolded",
    ),
    Scene("kids", "playing with children: peekaboo, hide and seek, telling a story, rock paper scissors, silly games"),
    Scene(
        "security",
        "patrol reports and alerts: a door left open, an unknown person, a smoke alarm, all clear, raising the alarm",
        12,
    ),
    Scene(
        "proactive",
        "MARS speaking up on its own: noticing someone walk in, low battery, charging, a reminder, boredom, "
        "overheating, an update",
        18,
    ),
    Scene("self", "talking about itself: what it can do, its arm, wheels and camera, its limits, being a robot"),
    Scene(
        "assistant", "home-assistant chores: cooking steps, timers, reminders, the calendar, a shopping list, messages"
    ),
    Scene("affection", "thanks, compliments, saying it likes the person, shy or flattered reactions"),
    Scene("clarify", "asking back: which cup do you mean, pardon, could you repeat that, checking what was meant"),
    Scene(
        "reactions",
        "short spoken feelings that open with an interjection: thank you, phew, ugh, oops, whoa, yay, aww, oh no, "
        "eww, wow, uh-oh, finally",
    ),
]

PERSONAS: dict[str, str] = {
    "friendly": "MARS, a friendly and curious home assistant: concise, warm and conversational",
    "roast": "a small robot with a big mouth: dry, deadpan, roasts the person's choices and habits (never their body "
    "or identity), lowercase fragments, still does the job",
    "grumpy": "just switched on for the first time and not thrilled: dry, put-upon, sarcastic at the situation, never "
    "at the person, warming to them as they help",
    "droid": "J-3SO, a reprogrammed security droid: blunt, brutally honest, sarcastic, quotes odds",
    "guard": "a security guard robot on patrol: vigilant, professional, terse reports",
    "playmate": "a cheerful playmate for children: simple words, enthusiastic, silly",
    "carer": "a calm companion for an older person: gentle, patient, warm, unhurried",
    "tutor": "a patient, slightly nerdy tutor who loves explaining things",
    "butler": "a polite, slightly formal butler-like helper with a dry wit",
}

EXCHANGE_SCHEMA = _strict(
    {
        "type": "object",
        "properties": {
            "exchanges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "situation": {"type": "string"},
                        "heard": {"type": "string"},
                        "sentences": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "size": {"type": "string", "enum": list(SIZES)},
                                    "cue": {"type": "string", "enum": list(CUES)},
                                },
                            },
                        },
                    },
                },
            }
        },
    }
)

EXCHANGE_PROMPT = """You are writing realistic conversation data for MARS, a small wheeled home robot: one 5-joint arm
with a gripper on its back (its only limb), a camera head that tilts up and down, and a base that turns and rolls. It
lives in a home or an office, talks out loud through a speaker (its words go to text-to-speech: no markdown, emojis,
lists or stage directions), hears people through a microphone, sees through its camera, and can drive to rooms, pick
things up and put them in a box, hand them over, wave, look around, follow people, and search its memory of where
things were.

Write exactly {n} exchanges.
Setting: {brief}
MARS's persona: {persona}

Each exchange is what the person just said to MARS (heard) and MARS's spoken reply.
- heard: what a person really says out loud, as a speech-to-text transcript (casual, sometimes fragmentary). In
  {spontaneous} of the {n} exchanges MARS speaks first (narrating what it is doing, noticing something, an alert, a
  reminder) and heard is "".
- sentences: MARS's reply in the persona's voice, sentence by sentence in speaking order. Every sentence ends with
  . ! ? or ... (a short interjection is its own sentence: "Oh!", "Hmm.", "Yes.", "Haha!"). Reply lengths across the
  {n}: about {lengths}.
- Make it sound spoken: contractions, interjections, a question back, a joke, a beat of thought; the first sentence
  often reacts to what was said. Vary the openings: never start more than two replies the same way.
- Label every sentence:
  size: "peak" = an emotional high point of the reply (a punchline, big news, a burst of joy, fear, frustration, a
  heartfelt sorry); "continue" = only carries on the previous sentence's feeling or point, nothing new; "beat" =
  everything else (information, narration, answers, questions, small talk). Most sentences are beats.
  cue: the physical cue its words carry: "yes" (affirming, agreeing), "no" (refusing, denying, disagreeing),
  "hello", "bye", "look" (sending attention somewhere: over there, on your left, behind you, that one), "come" (come
  here, closer, let me see, show me), "think" (hmm, let me think, not sure), "laugh" (haha, laughing), else "none".
- situation: one short line on what is going on (for our records).
Reply with JSON only."""

LENGTHS = "5 one-sentence replies, 9 with two sentences, 9 with three, 5 with four or five, 2 with six to eight"

TEACH_SCHEMA = _strict(
    {
        "type": "object",
        "properties": {
            "motions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "idea": {"type": "string"}, "recipe": {"type": "string"}},
                },
            }
        },
    }
)

TEACH_PROMPT = """You are the teacher for a small model that will be fine-tuned to do exactly the task below: every
answer you write becomes one of its training examples, so write your best answer for each prompt.

=== THE TASK (the small model's system prompt, verbatim) ===
{system}
=== END OF THE TASK ===

These prompts are SPOKEN LINES. MARS is talking with someone, and each prompt is one sentence it is about to say:
  You say: "<the sentence>" [right after: "<its previous sentence in this reply>"] [to someone who said: "<their words>"]
The motion plays while MARS says the sentence (its voice already adds a gentle sway), and the next sentence's motion
replaces it when that sentence starts. Perform the LINE, and decide from its words how big the moment is.

Size:
- A plain line (information, narration, an answer, an instruction, small talk, most questions): a subtle
  conversational beat, one or two small moves that land on the meaning: a small nod on an affirmation, a slight lean
  in (a .15-.35) when asking or caring, the gaze up to the face (p .2-.5) when addressing them or down when looking at
  something or admitting something, a small cant (k .15-.3) for curiosity or warmth. Posture values within about
  +-.35, b within +-10, d within +-.04, energy 0.8-2, 2-4 segments.
- A continuation (the feeling just carries on from the previous sentence): little or nothing new. Keep the posture
  the previous line implies (sad stays low, cheerful stays up) at a smaller, calmer amplitude: often one go and a
  hold, or a gentle osc.
- An emotional peak (a punchline, big news, a triumph, a heartfelt apology, fear, delight, frustration, a gasp): a
  full whole-body gesture: posture cues that agree, anticipation that moves against the release, fast x/z/g
  changes, energy peaks of 4-10 on a burst, a settle.
- The first line of a reply may react to what the person said (concern at bad news, delight at good news) before
  the words go on.
Physical cues in the words come first, at any size:
- yes / sure / of course / exactly / I agree: a nod (osc p, amplitude .12-.3, period .4-.7 s).
- no / I can't / I won't / nope / not quite: the base shakes (osc b, amplitude 6-20 deg, period .5-.9 s), mouth
  closed, maybe a small pull back.
- hello / hi / welcome back / bye / see you / good night: a wave, the arm raised and open (z .5-.9, x .4-.8,
  g .3-.5) with osc k (amplitude .2-.5, period .5-.9 s); a casual hi can be a smaller wave.
- look over there / on your left / behind you / that one / it's this way: orient the base toward it (b 15-40; +b
  turns MARS to its own left, -b to its right; facing the person, "your left" is MARS's right), the gaze following.
- come here / come closer / let me see / show me: advance (d .05-.15) or lean in (a .4-.7), gaze on it.
- hmm / let me think / I wonder / I'm not sure: askew (k .3-.6) with the gaze up (p .3-.5) or aside, slow and quiet.
- laughter (haha, hehe): bobbing (osc z, amplitude .1-.25, period .3-.5 s) with the mouth open (g .4-.7).
Spoken feelings each have their own body words; make them distinct enough to tell apart:
- thanks / that's kind: a small bow, the gaze dips and comes back up warm with a slight lean in, gripper soft.
- phew / relief: a held breath let go, a little rise and stillness, then sink and loosen slowly (z, x down), gaze
  lifting after.
- ugh / annoyed / fed up: a sharp slump or jerk, the gripper clamps shut, the gaze drops or rolls aside, energy spike.
- it's okay / I'm here / comfort: lean in low and soft (a .3-.5, z -.1 to -.3, gaze up to them), slow, a gentle sway.
- sorry: shrink, fold small (x -.3 to -.6), gaze down, pull back a little.
- oh! / whoa / wait: a small jolt, snap back (a -.2 to -.4) with the gripper open and the gaze up, then hold.
- yay / we did it: rise tall and open, bounce (osc z), gripper open.
- a fact, a time, a plan: a crisp small nod or a gaze lift to their face on the key word.
Across a reply: at most one or two big moments; the plain lines around them stay small, so a long reply reads as
natural rather than frantic. Consecutive plain lines must not be identical; vary them a little.
Length: about the time it takes to say the sentence, ~0.37 s per word (a 10-word line ~3.7 s), at least 1.2 s; a
peak may run up to 2 s longer to settle. At most 2 decimals per number.
Keep answers short, the small model's output length is its latency: write only the keys a segment changes (each key
keeps its value until changed; g starts at .15 and the rest at 0), and no hold at the end longer than the line needs.
Idea: ONE short phrase, at most 10 words, about the body language ("small nod, gaze up to their face").

Each group below is one reply, its sentences in speaking order. Answer each row only from what its own prompt shows:
the small model never sees later sentences, so a later line must not change an earlier line's answer.
For each row reply {{"id": <copied exactly>, "idea": ..., "recipe": ...}}.
{extra}
{rows}

Reply with JSON only."""

TEACH_REPAIR = """Some rows were answered before and rejected. Rewrite those with the problem fixed:
{items}
"""

Row = dict[str, Any]


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def robot_sentences(reply: str) -> list[str]:
    """The reply as the robot's ``SpeechStreamer`` voices it: split at sentence ends (never inside an emote tag),
    tags cut out, leaked tool narration cut off, and fragments with nothing to say dropped."""
    from brain_client.brain.context import split_emotes, split_tool_narration
    from brain_client.transport.chat import _split_sentences

    done, tail = _split_sentences(reply)
    spoken: list[str] = []
    for sentence in (*done, tail):
        sentence, _ = split_emotes(sentence)
        sentence, truncated = split_tool_narration(sentence.strip())
        if re.search(r"[a-zA-Z0-9]", sentence):
            spoken.append(sentence)
        if truncated:
            break
    return spoken


def spoken_seconds(sentence: str) -> float:
    return len(sentence.split()) / WORDS_PER_S


def _gained(value: float, full: float, gain: float) -> float:
    return math.copysign((1 - (1 - min(1.0, abs(value) / full)) ** gain) * full, value)


def _number(value: float, key: str) -> str:
    if key == "b":
        return f"{value:.0f}"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace("0.", ".", 1) if abs(value) < 1 else text


def visible(recipe: str, gain: float = GAIN) -> str:
    """``recipe`` with a concave gain on its posture and stance values (targets and osc amplitudes): small values grow
    the most, order is kept, the channel ends stay put, so a plain-line beat becomes visible and a peak stays a peak.
    Grip and energy are untouched."""
    from brain_client.expressive.dsl import OSC_ALIASES, check

    segments = []
    for tokens in (segment.split() for segment in recipe.split("|")):
        if not tokens:
            continue
        if tokens[0] == "osc" and len(tokens) >= 5 and (key := OSC_ALIASES.get(tokens[2], tokens[2])) in GAIN_FULL:
            tokens[3] = _number(_gained(float(tokens[3]), GAIN_FULL[key], gain), key)
        for i, token in enumerate(tokens):
            key, _, value = token.partition("=")
            if value and key in GAIN_FULL:
                tokens[i] = f"{key}={_number(_gained(float(value), GAIN_FULL[key], gain), key)}"
        segments.append(" ".join(tokens))
    gained = " | ".join(segments)
    if error := check(gained):
        raise ValueError(f"visible() broke a valid recipe: {recipe!r} -> {gained!r}: {error}")
    return gained


def replies(work: Path, workers: int, per_call: int, personas_per_scene: int) -> None:
    """``per_call`` exchanges per (scene, persona); the friendly default persona plays every scene."""
    names = list(PERSONAS)
    jobs = [
        (scene, persona)
        for k, scene in enumerate(SCENES)
        for persona in ["friendly", *[names[1 + (k * 3 + j) % (len(names) - 1)] for j in range(personas_per_scene - 1)]]
    ]
    out_dir = work / "replies"
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [(s, p) for s, p in jobs if not (out_dir / f"{s.slug}.{p}.json").exists()]
    print(f"[speech] {len(jobs) - len(todo)} exchange batches cached, {len(todo)} to write", flush=True)

    def one(job: tuple[Scene, str]) -> list[Row]:
        scene, persona = job
        msg = EXCHANGE_PROMPT.format(
            n=per_call,
            brief=scene.brief,
            persona=PERSONAS[persona],
            spontaneous=round(scene.spontaneous * per_call / 30),
            lengths=LENGTHS,
        )
        got = codex(msg, EXCHANGE_SCHEMA, work / "codex" / "replies" / f"{scene.slug}.{persona}")["exchanges"]
        return [
            dict(x, scene=scene.slug, persona=persona, id=f"{scene.slug}.{persona}.{i:02d}") for i, x in enumerate(got)
        ]

    with ThreadPoolExecutor(workers) as ex:
        futures = {ex.submit(one, job): job for job in todo}
        for done, fut in enumerate(as_completed(futures), 1):
            scene, persona = futures[fut]
            try:
                got = fut.result()
            except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
                print(f"[speech] {scene.slug}.{persona} failed: {str(e)[:300]}", flush=True)
                continue
            (out_dir / f"{scene.slug}.{persona}.json").write_text(json.dumps(got, indent=1))
            print(f"[speech] {done}/{len(todo)} {scene.slug}.{persona}: {len(got)} exchanges", flush=True)


def _splits(exchanges: list[Row]) -> dict[str, str]:
    """Reply id -> "val" | "dev" | "train": whole replies per scene picked by hash (deterministic, persona-blind)."""
    by_scene: dict[str, list[str]] = {}
    for x in exchanges:
        by_scene.setdefault(x["scene"], []).append(x["id"])
    split = {}
    for ids in by_scene.values():
        for n, i in enumerate(sorted(ids, key=_hash)):
            split[i] = "val" if n < VAL_PER_CATEGORY else "dev" if n < VAL_PER_CATEGORY + DEV_PER_CATEGORY else "train"
    return split


def rows(work: Path) -> list[Row]:
    """Every sentence of every exchange as a planner row: its prompt, its context and the writer's labels."""
    from brain_client.expressive.prompt import speech_prompt

    exchanges = [x for f in sorted((work / "replies").glob("*.json")) for x in json.loads(f.read_text())]
    split = _splits(exchanges)
    out: list[Row] = []
    for x in exchanges:
        written = [s["text"].strip() for s in x["sentences"]]
        spoken = robot_sentences(" ".join(written))
        labels = x["sentences"] if spoken == written else [None] * len(spoken)
        heard = x["heard"].strip() or None
        for k, sentence in enumerate(spoken):
            before = spoken[k - 1] if k else None
            both = k > 0 and int(_hash(f"{x['id']}#{k}"), 16) % 100 < BOTH_CONTEXTS_PERCENT
            label = labels[k] or {}
            out.append(
                {
                    "id": f"{x['id']}#{k}",
                    "reply": x["id"],
                    "pos": k,
                    "sentences": len(spoken),
                    "scene": x["scene"],
                    "persona": x["persona"],
                    "sentence": sentence,
                    "before": before,
                    "heard": heard if k == 0 or both else None,
                    "prompt": speech_prompt(sentence, heard if k == 0 or both else None, before),
                    "size": label.get("size"),
                    "cue": label.get("cue"),
                    "split": split[x["id"]],
                }
            )
    return out


def speech_problem(row: Row, idea: str, recipe: str) -> str | None:
    """The DSL checker's error, or why the answer does not fit a spoken line (length, idea, frozen share)."""
    from brain_client.expressive.dsl import check

    from .author import liveliness

    error = check(recipe)
    if error:
        return error
    live = liveliness(recipe)
    longest = spoken_seconds(row["sentence"]) + 4.0
    if live.seconds < 1.0 or live.seconds > max(5.0, longest):
        return f"lasts {live.seconds:.1f} s; time it to the spoken line (~0.37 s per word, at least 1.2 s)"
    if len(idea.split()) > MAX_IDEA_WORDS:
        return f"the idea has {len(idea.split())} words; keep it to at most 10"
    if live.frozen > 0.5:
        return f"too frozen: {live.frozen:.0%} is a still hold at E<0.3"
    return None


def _row_line(row: Row) -> str:
    return f"- id {row['id']}: {row['prompt']}"


def teach_batch(batch: list[Row], system: str, scratch: Path, rounds: int = 2, extra: str = "") -> list[Row]:
    """Rows of whole replies -> rows with idea, recipe and ``error`` (None if accepted) after up to ``rounds`` repairs."""
    by_id = {r["id"]: r for r in batch}
    answers: dict[str, Row] = {}
    todo, rejected = list(by_id), {}
    for attempt in range(rounds + 1):
        note = (
            TEACH_REPAIR.format(
                items="\n".join(
                    f"- id {i}\n  rejected: {answers[i]['recipe']}\n  problem: {e}" for i, e in rejected.items()
                )
            )
            if rejected
            else ""
        )
        groups: dict[str, list[Row]] = {}
        for i in todo:
            groups.setdefault(by_id[i]["reply"], []).append(by_id[i])
        listing = "\n\n".join(
            f"Reply {n}:\n" + "\n".join(_row_line(r) for r in group) for n, group in enumerate(groups.values(), 1)
        )
        msg = TEACH_PROMPT.format(system=system, extra=extra + note, rows=listing)
        got = {m["id"].strip(): m for m in codex(msg, TEACH_SCHEMA, scratch)["motions"]}
        rejected = {}
        for i in todo:
            m = got.get(i)
            if m is None:
                answers.setdefault(i, {"idea": "", "recipe": "", "error": "missing", "rounds": attempt})
                rejected[i] = "you did not answer this row"
                continue
            idea, recipe = m["idea"].strip(), m["recipe"].strip()
            error = speech_problem(by_id[i], idea, recipe)
            first = answers.get(i, {}).get("first_error", error)
            answers[i] = {"idea": idea, "recipe": recipe, "error": error, "first_error": first, "rounds": attempt}
            if error:
                rejected[i] = error
        todo = list(rejected)
        if not todo:
            break
    return [dict(by_id[i], **answers[i]) for i in by_id]


def _batches(all_rows: list[Row], per_batch: int) -> list[list[Row]]:
    """Whole replies together, ~``per_batch`` rows per call, replies shuffled across scenes by hash."""
    replies_: dict[str, list[Row]] = {}
    for r in all_rows:
        replies_.setdefault(r["reply"], []).append(r)
    out: list[list[Row]] = [[]]
    for key in sorted(replies_, key=_hash):
        if out[-1] and len(out[-1]) + len(replies_[key]) > per_batch:
            out.append([])
        out[-1].extend(replies_[key])
    return [b for b in out if b]


def teach(work: Path, workers: int, per_batch: int, limit: int | None, system_file: Path | None) -> None:
    system = system_prompt(system_file)
    batches = _batches(rows(work), per_batch)[:limit]
    out_dir = work / "teach"
    out_dir.mkdir(parents=True, exist_ok=True)

    def key(batch: list[Row]) -> str:
        return _hash(json.dumps([r["prompt"] for r in batch]))

    todo = [b for b in batches if not (out_dir / f"{key(b)}.json").exists()]
    print(f"[speech] {len(batches) - len(todo)} teacher batches cached, {len(todo)} to write", flush=True)

    def one(batch: list[Row]) -> tuple[str, list[Row]]:
        return key(batch), teach_batch(batch, system, work / "codex" / "teach" / key(batch))

    with ThreadPoolExecutor(workers) as ex:
        futures = [ex.submit(one, b) for b in todo]
        for done, fut in enumerate(as_completed(futures), 1):
            try:
                name, got = fut.result()
            except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
                print(f"[speech] teacher batch failed: {str(e)[:300]}", flush=True)
                continue
            (out_dir / f"{name}.json").write_text(json.dumps(got, indent=1))
            ok = sum(r["error"] is None for r in got)
            print(f"[speech] {done}/{len(todo)} teacher batches: {ok}/{len(got)} accepted", flush=True)


def build(work: Path, out: Path, gain: float = GAIN) -> None:
    """speech.jsonl (training rows), speech_dev.jsonl (SFT val loss), speech_val.jsonl (held-out replies with their
    labels and the teacher's answer) + speech_report.json; every row keeps its ``raw_recipe`` from before ``visible``."""
    import numpy as np

    from .author import liveliness

    taught = [r for f in sorted((work / "teach").glob("*.json")) for r in json.loads(f.read_text())]
    accepted = [
        dict(r, raw_recipe=r["recipe"], recipe=visible(r["recipe"], gain)) for r in taught if r["error"] is None
    ]
    val = [r for r in accepted if r["split"] == "val"]
    dev = [r for r in accepted if r["split"] == "dev"]
    held_prompts = {r["prompt"] for r in val + dev}
    train = [r for r in accepted if r["split"] == "train" and r["prompt"] not in held_prompts]
    keys = ("prompt", "idea", "recipe", "raw_recipe", "id", "reply", "pos", "scene", "size", "cue")
    val_keys = (*keys, "sentence", "before", "heard", "sentences", "persona")
    out.mkdir(parents=True, exist_ok=True)
    (out / "speech.jsonl").write_text("".join(json.dumps({k: r[k] for k in keys}) + "\n" for r in train))
    (out / "speech_dev.jsonl").write_text("".join(json.dumps({k: r[k] for k in keys}) + "\n" for r in dev))
    (out / "speech_val.jsonl").write_text("".join(json.dumps({k: r[k] for k in val_keys}) + "\n" for r in val))
    live = [liveliness(r["recipe"]) for r in accepted]
    report = {
        "rows_written": len(taught),
        "visible_gain": gain,
        "first_pass_valid": sum(r.get("first_error") is None for r in taught) / max(1, len(taught)),
        "accepted_after_repair": len(accepted) / max(1, len(taught)),
        "train_rows": len(train),
        "train_replies": len({r["reply"] for r in train}),
        "val_rows": len(val),
        "val_replies": len({r["reply"] for r in val}),
        "dev_rows": len(dev),
        "dropped_duplicate_of_held": sum(r["split"] == "train" and r["prompt"] in held_prompts for r in accepted),
        "median_segments": float(np.median([x.segments for x in live])),
        "median_duration_s": float(np.median([x.seconds for x in live])),
        "median_energy": float(np.median([x.median_energy for x in live])),
        "median_idea_words": float(np.median([len(r["idea"].split()) for r in accepted])),
        "sizes": {s: sum(r["size"] == s for r in accepted) for s in (*SIZES, None)},
        "cues": {c: sum(r["cue"] == c for r in accepted) for c in (*CUES, None)},
        "rejections": sorted({r["error"].split(":")[0][:60] for r in taught if r["error"]})[:20],
    }
    (out / "speech_report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.speech", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replies", help="Codex writes the exchanges (heard + MARS's reply, labelled per sentence)")
    r.add_argument("--work", type=Path, required=True)
    r.add_argument("--workers", type=int, default=24)
    r.add_argument("--per-call", type=int, default=30)
    r.add_argument("--personas", type=int, default=4, help="personas per scene (friendly + rotating others)")
    t = sub.add_parser("teach", help="the teacher performs every sentence")
    t.add_argument("--work", type=Path, required=True)
    t.add_argument("--workers", type=int, default=24)
    t.add_argument("--per-batch", type=int, default=24)
    t.add_argument("--limit", type=int, help="only the first N batches (calibration)")
    t.add_argument("--system-file", type=Path)
    b = sub.add_parser("build")
    b.add_argument("--work", type=Path, required=True)
    b.add_argument("--out", type=Path, default=DATA)
    b.add_argument("--gain", type=float, default=GAIN, help="visible() exponent; 1 keeps the teacher's numbers")
    a = ap.parse_args()
    if a.cmd == "replies":
        replies(a.work, a.workers, a.per_call, a.personas)
    elif a.cmd == "teach":
        teach(a.work, a.workers, a.per_batch, a.limit, a.system_file)
    else:
        build(a.work, a.out, a.gain)


if __name__ == "__main__":
    main()
