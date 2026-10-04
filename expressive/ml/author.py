"""Teacher dataset for the distilled planner, authored with the Codex CLI (gpt-6-astra).

  python -m ml.author families --work W     # ~450 scenario families x 7 prompts (W/families.json)
  python -m ml.author recipes  --work W     # one recipe per prompt, checked, repaired, lively-calibrated
  python -m ml.author seed     --work W     # 118 hand-curated rows (seed_prompts.tsv): self-critique with a checker tool
  python -m ml.author val2     --work W     # a second, independent teacher label for the 40 val prompts
  python -m ml.author probes   --work W     # the teacher's own answers to the probe prompts (12 takes each)
  python -m ml.author real     --work W     # the teacher's recipes for the 12 held-out real emotions
  python -m ml.author build    --work W     # -> distill_data/{dataset,val}.jsonl + report.json

Every call is resumable: finished batches are cached under W, keyed by their prompts only, so delete W/recipes (or
W/seed, ...) to regenerate after changing the house style or the system prompt. Recipes are written with the FROZEN
planner prompt (brain_client.expressive.prompt), validated with the core DSL checker, and repaired up to twice with the
checker's error fed back.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL = "gpt-6-astra"
PROMPTS_PER_FAMILY = 7

# Concepts of the out-of-distribution probes: never in training data (Binh's OOD-core list, keyword form).
OOD_BLOCK = re.compile(r"sneez|startl|drunk|tipsy|dizz|toddler|stalk|pounc|heartbr|ecstat", re.I)


@dataclass(frozen=True)
class Category:
    slug: str
    families: int
    brief: str


CATEGORIES: list[Category] = [
    Category(
        "emotion/positive",
        28,
        "positive emotions and moods: joy, contentment, pride, gratitude, relief, "
        "amusement, awe, tenderness, hope, playfulness, triumph, serenity ... Each family is ONE emotion; its "
        "prompts climb an intensity ladder (subtle, clear, extreme) and vary the situation that causes it.",
    ),
    Category(
        "emotion/negative",
        30,
        "negative emotions and moods: sadness, grief, anger, fear, anxiety, disgust, "
        "shame, guilt, loneliness, frustration, boredom, jealousy, despair, dread, irritation ... One emotion per "
        "family, prompts climb an intensity ladder (subtle, clear, extreme) and vary the cause.",
    ),
    Category(
        "emotion/complex",
        24,
        "mixed and social feelings: bittersweet, nostalgic, embarrassed, smug, "
        "sheepish, awkward, suspicious, skeptical, determined, conflicted, flustered, defiant, wistful, "
        "starstruck, homesick ... One feeling per family with intensity and context variants.",
    ),
    Category(
        "reaction",
        30,
        "fast reactions and reflexes: a double-take, a flinch from heat, a wince, recoiling "
        "from a smell, a gasp, a shiver, a hiccup, an itch, a cough, a jolt of static, dodging a thrown object, "
        "a near miss, a hot drink spilled, a sudden realization ... One reaction per family.",
    ),
    Category(
        "social/greeting",
        14,
        "a companion robot meeting people: greeting a stranger, an old friend, a "
        "child, a group, a pet, someone coming home, a shy guest, someone you missed; and saying goodbye, "
        "seeing someone off, a reluctant farewell, waving someone away kindly ... One social beat per family.",
    ),
    Category(
        "social/conversation",
        30,
        "a companion robot in conversation: listening attentively, agreeing, "
        "politely disagreeing, being confused by a question, thinking before answering, explaining something, "
        "asking a question, apologising, thanking, encouraging, consoling, teasing, joking, interrupting, "
        "waiting for an answer, being ignored, being complimented, being scolded, keeping a secret, "
        "changing the subject, losing the thread ... One conversational beat per family.",
    ),
    Category(
        "social/task",
        22,
        "a home helper robot around its tasks: celebrating a completed task, failing a "
        "task, dropping something, trying again, asking for help, offering an object, presenting a result "
        "proudly, noticing a person enter, noticing a mess, searching for a lost item, waiting for "
        "instructions, being interrupted mid-task, refusing an unsafe request, double-checking work ... "
        "One beat per family.",
    ),
    Category(
        "body/state",
        26,
        "bodily states and sensations: tired, sore, cold, too hot, hungry, full, sick, "
        "itchy, stiff, stretching after sleep, holding a breath, out of breath, aching back, ticklish, "
        "sleepy (not a toddler), restless legs, balancing on one foot, carrying something heavy ... "
        "One state per family with intensity and context variants.",
    ),
    Category(
        "animal",
        40,
        "animals and their behaviour: dogs (begging, wagging, fetching, guarding), cats "
        "(grooming, kneading, aloof), birds (pecking, preening, an owl turning, a parrot bobbing), a turtle "
        "hiding in its shell, a crab scuttling, a snake rearing, a meerkat on watch, a sloth, a horse, a "
        "penguin waddling, a hummingbird hovering, an elephant, a giraffe, a frog, a squirrel burying a nut, "
        "a chicken, a puppy chasing its tail ... One animal behaviour per family. No stalking or pouncing.",
    ),
    Category(
        "character",
        34,
        "characters and personas: a proud butler, a pirate captain, a grumpy old man, a "
        "shy child, a diva, a detective, a drill sergeant, a ballet dancer, a zombie, a ghost, a superhero "
        "landing, a nervous intern, a magician, a royal waving, a cowboy, a vampire, a sleepy grandma, a "
        "mad scientist, a mime, a sumo wrestler, a robot from an old film, a sports coach, a librarian "
        "shushing ... One character per family, prompts place it in different moments.",
    ),
    Category(
        "robot/state",
        24,
        "the robot's own machine states shown through body language: booting up, "
        "shutting down, low battery, charging, overheating, a software update, connecting to wifi, a lost "
        "connection, scanning a room, calibrating, a sensor glitch, recovering from an error, processing a hard "
        "request, idling, standby, a firmware crash and reboot, lagging, buffering ... One state per family.",
    ),
    Category(
        "metaphor",
        30,
        "metaphors and images to embody: a wilting flower, a coiled spring, a balloon "
        "deflating, a candle flickering out, a storm brewing, a pendulum, a melting ice cream, a sprouting seed, "
        "a kettle about to boil, a leaf in the wind, a heavy anchor, a rubber band snapping, a jack-in-the-box, "
        "a tree in a gale, a lighthouse sweeping, a puppet with cut strings ... One image per family.",
    ),
    Category(
        "event/buildup",
        34,
        "build-up then release actions with clear timing: hammering a nail, knocking "
        "on a door, throwing a ball, kicking, a karate chop, a cough, a hiccup fit, barking, lifting something "
        "heavy, popping a balloon, a golf swing, a whip crack, a drum hit, a bow-and-arrow release, stamping "
        "a document, slam dunk, cannonball dive, a big laugh bursting out ... One action per family; the "
        "prompts describe the wind-up and the release.",
    ),
    Category(
        "story/multiphase",
        30,
        "short stories with two to four phases and a turn: opening a gift and "
        "finding socks, waiting for a bus that finally comes, losing then finding your keys, falling asleep "
        "and jolting awake in class, getting bad news then good news, trying a sour sweet, watching a scary "
        "film, winning a race at the last second, a tower of blocks wobbling and falling, getting caught "
        "eating a cookie ... One story per family; each prompt is a different story in that family's theme.",
    ),
    Category(
        "music/dance",
        18,
        "music and rhythm: grooving to funk, a slow waltz, headbanging to metal, "
        "conducting an orchestra, a lullaby sway, a disco move, keeping time to a metronome, a drum roll, a "
        "marching band, a sad violin, a salsa step, jazz hands, a victory dance ... One style per family.",
    ),
    Category(
        "gesture/arm",
        22,
        "expressive gestures a robot with ONE arm and a gripper could make: waving hello, "
        "beckoning come here, shooing away, a shrug, pointing at something, presenting with a flourish, a "
        "high five, covering its face, a thumbs-up-like raise, a fist pump, reaching out for a hug, a "
        "salute, pinching its 'chin' in thought, scratching its head, raising a hand to ask ... One gesture "
        "per family with different contexts and intensities.",
    ),
    Category(
        "play",
        16,
        "games and play: peekaboo, hide and seek, playing fetch, tag, a staring contest, "
        "rock paper scissors, a tickle fight, chasing a laser dot, pretending to be asleep, a magic trick "
        "reveal, a surprise party jump-out, playing statues ... One game per family.",
    ),
]


def category(family: str) -> str:
    return next((c.slug for c in CATEGORIES if family.startswith(c.slug + "/")), family.split("/")[0])


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def codex(
    prompt: str, schema: dict[str, Any], scratch: Path, effort: str = "xhigh", timeout: float = 3600
) -> dict[str, Any]:
    """One non-interactive Codex call whose final message must match ``schema``."""
    scratch.mkdir(parents=True, exist_ok=True)
    key = _hash(prompt + effort)
    schema_path, out_path = scratch / f"schema-{key}.json", scratch / f"out-{key}.json"
    schema_path.write_text(json.dumps(schema))
    cmd = [
        "codex",
        "exec",
        "-m",
        MODEL,
        "-c",
        f"model_reasoning_effort={effort}",
        "-s",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(scratch),
        "--output-schema",
        str(schema_path),
        "-o",
        str(out_path),
        "-",
    ]
    run = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=timeout)
    if run.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"codex failed ({run.returncode}): {run.stderr[-2000:]}")
    return json.loads(out_path.read_text())


def _strict(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"])
        for sub in schema["properties"].values():
            _strict(sub)
    if schema.get("type") == "array":
        _strict(schema["items"])
    return schema


FAMILY_SCHEMA = _strict(
    {
        "type": "object",
        "properties": {
            "families": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "family": {"type": "string"},
                        "prompts": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
        },
    }
)

FAMILY_PROMPT = """You are building a training set for a small model that turns a text prompt into expressive body
language for MARS, a small wheeled home robot with one 5-joint arm and a gripper on its back (its only limb), a camera
head that only tilts up and down, and a base that can turn and roll a little. The prompts must be things a person or
an agent might ask MARS to express.

Category: {slug}
Scope: {brief}

Write exactly {n} distinct families for this category, each with exactly {k} prompts.
- family: a short slug "{slug}/<name>" (lowercase, hyphens).
- Every prompt has the form "<bare concept>. <one vivid second-person sentence that sets up the situation>."
  e.g. "relieved. The test results came back fine and your whole body unwinds."
  e.g. "a dog begging at the table. You sit very still and stare at the sandwich, hoping."
- Within a family, vary intensity (subtle / clear / extreme), cause, and phrasing; the bare concepts may repeat
  with a qualifier ("slightly annoyed", "furious") but the sentences must all differ.
- Concrete, physical, imaginable situations. No prompt longer than 30 words.
- Never use these concepts anywhere (they are held out for evaluation): sneezing, being startled, drunk or tipsy,
  dizzy, toddlers, stalking or pouncing, heartbroken, ecstatic. Also do not write the exact prompts "nodding yes",
  "shaking your head no", "bowing deeply", "looking up at the stars", "cowering in fear", "jumping for joy",
  "yawning widely", "checking both ways" (related ideas are fine).
{avoid}
Reply with JSON only."""


def families(work: Path, workers: int) -> None:
    out = work / "families.json"
    have: dict[str, list[str]] = json.loads(out.read_text()) if out.exists() else {}

    def one(cat: Category) -> list[dict[str, Any]]:
        msg = FAMILY_PROMPT.format(slug=cat.slug, brief=cat.brief, n=cat.families, k=PROMPTS_PER_FAMILY, avoid="")
        return codex(msg, FAMILY_SCHEMA, work / "codex" / "families")["families"]

    todo = [c for c in CATEGORIES if not any(f.startswith(c.slug + "/") for f in have)]
    with ThreadPoolExecutor(workers) as ex:
        futures = {ex.submit(one, c): c for c in todo}
        for fut in as_completed(futures):
            cat = futures[fut]
            try:
                got = fut.result()
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
                print(f"[families] {cat.slug} failed: {e}", flush=True)
                continue
            for fam in got:
                name = fam["family"] if fam["family"].startswith(cat.slug + "/") else f"{cat.slug}/{fam['family']}"
                have[name] = [p.strip() for p in fam["prompts"] if p.strip() and not OOD_BLOCK.search(p)]
            out.write_text(json.dumps(have, indent=1))
            print(f"[families] {cat.slug}: {len(got)} families ({len(have)} total)", flush=True)
    print(f"[families] {len(have)} families, {sum(map(len, have.values()))} prompts -> {out}")


RECIPE_SCHEMA = _strict(
    {
        "type": "object",
        "properties": {
            "motions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "idea": {"type": "string"},
                        "recipe": {"type": "string"},
                    },
                },
            }
        },
    }
)

RECIPE_PROMPT = """You are the teacher for a small model that will be fine-tuned to do exactly the task below: every
answer you write becomes one of its training examples, so write your best answer for each prompt.

=== THE TASK (the small model's system prompt, verbatim) ===
{system}
=== END OF THE TASK ===

House style (what makes MARS feel alive rather than choreographed):
- Keep the body alive: energy mostly 0.8-3 between events, with short peaks of 5-10 on bursts, snaps, jolts,
  overflowing excitement and trembling; reserve E below 0.4 for deliberate stillness, and keep frozen holds (hold at
  E < 0.3) under a third of the motion unless the prompt is about stillness, sleep or hiding.
- Rhythm: use osc where something repeats or sways (nod p, bob z, sway k, lean a, turn b, chatter g); not every
  recipe needs one.
- Commit to the posture with agreeing cues; make big x/z/g changes fast; give events anticipation (the wind-up moves
  AGAINST the release) and a settle; intensity ladders (subtle / clear / extreme) must differ visibly in amplitude,
  speed and energy.
- Use the stance channels when they mean something: b to look away or turn to face, d to step toward or back off.
  The grip is the mouth: gasps open it, chatter oscillates it, determination clamps it shut.
- Typical length 3-10 s; multi-phase stories may run 10-20 s. At most 2 decimals per number. The idea is one sentence
  about the body language.
- Different prompts must get genuinely different motions, even within one family.
{extra}
For each prompt reply {{"prompt": <copied exactly>, "idea": ..., "recipe": ...}}.
Prompts:
{prompts}

Reply with JSON only."""

REPAIR_NOTE = """Some of these prompts were answered before and the answer was rejected. For those, the rejected recipe
and the reason follow; write a corrected recipe that keeps the idea but fixes the problem:
{items}
"""

STILLNESS = re.compile(
    r"still|freez|frozen|statue|sleep|asleep|nap|doz|motionless|meditat|breath|standby|idle|"
    r"shut|power|dead|pretend|stare|staring|hid|serene|peace|tranquil|calm|patien|wait|silent|"
    r"quiet|stuck|numb|hourglass|snail|sloth|anchor|bored|lazy|hibernat|faint|lifeless",
    re.I,
)
LOW_AROUSAL = re.compile(
    r"sad|gloom|grie|tired|exhaust|weary|depress|melanchol|sorrow|lonel|despair|defeat|resign|"
    r"drain|heavy|sluggish|mourn|wistful|nostalg|somber|sombre|dejected|downcast|forlorn|"
    r"disappoint|deflat|wilt|melt|sink|slump|drowsy|sleepy|low battery|shutting|fading",
    re.I,
)
MAX_FROZEN = 0.3
MIN_MEDIAN_ENERGY = 0.6


@dataclass(frozen=True)
class Liveliness:
    seconds: float
    segments: int
    median_energy: float
    frozen: float


def liveliness(recipe: str) -> Liveliness:
    """Duration, segment count, median energy and the share of frames frozen (posture still at E < 0.3)."""
    import numpy as np

    from brain_client.expressive.channels import FPS, HIGH, LOW, Ch
    from brain_client.expressive.dsl import expand

    frames = expand(recipe)
    posture = frames[:, : Ch.ENERGY] / (HIGH[: Ch.ENERGY] - LOW[: Ch.ENERGY])
    still = np.concatenate([[True], np.abs(np.diff(posture, axis=0)).max(1) < 1e-4])
    frozen = float(((frames[:, Ch.ENERGY] < 0.3) & still).mean())
    segments = len([s for s in recipe.split("|") if s.strip()])
    return Liveliness((len(frames) - 1) / FPS, segments, float(np.median(frames[:, Ch.ENERGY])), frozen)


def problem(prompt: str, recipe: str) -> str | None:
    """The DSL checker's error, or a liveliness rejection (Binh's lively calibration), else None."""
    from brain_client.expressive.dsl import check

    error = check(recipe)
    if error or STILLNESS.search(prompt):
        return error
    live = liveliness(recipe)
    if live.frozen > MAX_FROZEN:
        return f"too frozen: {live.frozen:.0%} of the motion is a still hold at E<0.3 (keep it under 30%)"
    if live.median_energy < MIN_MEDIAN_ENERGY and not LOW_AROUSAL.search(prompt):
        return f"too flat: median energy {live.median_energy:.2f} (keep it >= 0.6 unless the prompt is about stillness)"
    return None


def system_prompt(path: Path | None) -> str:
    """The FROZEN planner prompt: core's prompt module, or a file holding the same text."""
    if path is not None:
        return path.read_text().strip()
    from brain_client.expressive.prompt import SYSTEM

    return SYSTEM


Row = dict[str, Any]


def write_batch(
    prompts: dict[str, str], system: str, scratch: Path, extra: str = "", rounds: int = 2, effort: str = "xhigh"
) -> list[Row]:
    """{prompt: family} -> rows with idea, recipe and ``error`` (None if accepted) after up to ``rounds`` repairs."""
    rows: dict[str, Row] = {}
    todo, rejected = list(prompts), {}
    for attempt in range(rounds + 1):
        note = (
            REPAIR_NOTE.format(
                items="\n".join(f"- {p}\n  rejected: {rows[p]['recipe']}\n  problem: {e}" for p, e in rejected.items())
            )
            if rejected
            else ""
        )
        msg = RECIPE_PROMPT.format(system=system, extra=extra + note, prompts="\n".join(f"- {p}" for p in todo))
        got = {m["prompt"].strip(): m for m in codex(msg, RECIPE_SCHEMA, scratch, effort)["motions"]}
        rejected = {}
        for p in todo:
            m = got.get(p.strip())
            if m is None:
                rows.setdefault(
                    p,
                    {
                        "prompt": p,
                        "family": prompts[p],
                        "idea": "",
                        "recipe": "",
                        "rounds": attempt,
                        "error": "missing from the answer",
                    },
                )
                rejected[p] = "you did not answer this prompt"
                continue
            error = problem(p, m["recipe"])
            rows[p] = {
                "prompt": p,
                "family": prompts[p],
                "idea": m["idea"].strip(),
                "recipe": m["recipe"].strip(),
                "rounds": attempt,
                "error": error,
                "first_error": rows.get(p, {}).get("first_error", error),
            }
            if error:
                rejected[p] = error
        todo = list(rejected)
        if not todo:
            break
    return list(rows.values())


def _batches(families_: dict[str, list[str]], per_batch: int) -> list[dict[str, str]]:
    """Whole families together (so intensity ladders are written side by side), ~``per_batch`` prompts per call."""
    out: list[dict[str, str]] = [{}]
    for family, prompts in families_.items():
        if out[-1] and len(out[-1]) + len(prompts) > per_batch:
            out.append({})
        out[-1].update({p: family for p in prompts})
    return [b for b in out if b]


def run_batches(
    batches: list[dict[str, str]], system: str, out_dir: Path, workers: int, extra: str = "", effort: str = "xhigh"
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [b for b in batches if not (out_dir / f"{_hash(json.dumps(sorted(b)))}.json").exists()]
    print(f"[author] {len(batches) - len(todo)} batches cached, {len(todo)} to write ({workers} workers)", flush=True)

    def one(batch: dict[str, str]) -> tuple[str, list[Row]]:
        key = _hash(json.dumps(sorted(batch)))
        return key, write_batch(batch, system, out_dir.parent / "codex" / out_dir.name / key, extra, effort=effort)

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        futures = [ex.submit(one, b) for b in todo]
        for fut in as_completed(futures):
            try:
                key, rows = fut.result()
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
                print(f"[author] batch failed: {str(e)[:300]}", flush=True)
                continue
            (out_dir / f"{key}.json").write_text(json.dumps(rows, indent=1))
            done += 1
            ok = sum(r["error"] is None for r in rows)
            print(f"[author] {done}/{len(todo)} batches: {ok}/{len(rows)} accepted", flush=True)


SEED_PROMPTS = Path(__file__).with_name("seed_prompts.tsv")

SELF_CRITIQUE = """
This batch is the hand-curated core of the dataset: quality matters more than speed. Work like this:
1. Draft a recipe for every prompt.
2. Check each one with the validator (run it in a shell; it prints the duration, segment count, median energy and
   frozen share, or why it is rejected):
     {checker} --prompt '<prompt>' '<recipe>'
3. Critique each draft against its prompt as a choreographer would: does every move go the right DIRECTION on this
   body (gaze down vs up, lean in vs pull back, tall vs low, open vs closed)? Is the ORDER and TIMING right (onset ->
   peak -> settle, anticipation against the release, fast ear-like x/z/g changes)? Do the posture cues agree? Would a
   viewer name the prompt from the motion alone? Is it alive without being busy?
4. Revise, re-check, and repeat steps 2-4 for at least three rounds of critique.
Answer only with recipes the validator accepts.
"""


def seed(work: Path, workers: int, system_file: Path | None, checker: str) -> None:
    rows = [line.split("\t", 1) for line in SEED_PROMPTS.read_text().splitlines() if line.strip()]
    prompts = {prompt: family for family, prompt in rows}
    batches = [dict(list(prompts.items())[i : i + 10]) for i in range(0, len(prompts), 10)]
    run_batches(batches, system_prompt(system_file), work / "seed", workers, SELF_CRITIQUE.format(checker=checker))


VAL_SIZE = 40
VAL_NOTE = "\nThese prompts get a second, independent take: write it fresh, as if you had never seen them.\n"


def val_prompts(work: Path) -> dict[str, str]:
    """40 held-out prompts, one per family, spread evenly over the categories (deterministic)."""
    import random

    fams: dict[str, list[str]] = json.loads((work / "families.json").read_text())
    rng = random.Random(7)
    by_cat: dict[str, list[str]] = {}
    for family in fams:
        by_cat.setdefault(category(family), []).append(family)
    shuffled = [rng.sample(group, len(group)) for group in by_cat.values()]
    picked = [f for row in itertools.zip_longest(*shuffled) for f in row if f][:VAL_SIZE]
    return {rng.choice(fams[f]): f for f in picked}


def second_labels(work: Path, workers: int, system_file: Path | None) -> None:
    val = val_prompts(work)
    batches = [dict(list(val.items())[i : i + 10]) for i in range(0, len(val), 10)]
    run_batches(batches, system_prompt(system_file), work / "val2", workers, VAL_NOTE)


def teacher_probes(work: Path, samples: int, workers: int, system_file: Path | None) -> None:
    """The teacher's own answers to the probe prompts, ``samples`` independent calls each (for the comparison)."""
    from brain_client.expressive.probes import PROBES

    prompts = {p.prompt: "probe" for p in PROBES}
    system = system_prompt(system_file)
    out = work / "teacher_probes.json"

    def one(k: int) -> list[Row]:
        return write_batch(prompts, system, work / "codex" / "probes" / str(k), VAL_NOTE + f"(take {k})\n", rounds=2)

    got: dict[str, list[str | None]] = {p: [] for p in prompts}
    with ThreadPoolExecutor(workers) as ex:
        for rows in ex.map(one, range(samples)):
            for r in rows:
                got[r["prompt"]].append(r["recipe"] if r["error"] is None else None)
    out.write_text(json.dumps(got, indent=1))
    print(f"[author] teacher probe recipes ({samples} per prompt) -> {out}")


def teacher_real(work: Path, system_file: Path | None) -> None:
    """The teacher's recipes for the 12 held-out real emotions' captions (the planner eval's real-clip check)."""
    from .retarget import EMOTIONS, HELD_OUT, pollen_captions

    captions = {pollen_captions(EMOTIONS)[h]: h for h in HELD_OUT}
    rows = write_batch(dict.fromkeys(captions, "held-out"), system_prompt(system_file), work / "codex" / "real")
    out = work / "teacher_real.json"
    out.write_text(
        json.dumps({captions[r["prompt"]]: r["recipe"] if r["error"] is None else None for r in rows}, indent=1)
    )
    print(f"[author] teacher recipes for the held-out clips -> {out}")


def probe_concepts() -> set[str]:
    """Bare concepts of the probe prompts ("sneezing", "nodding yes", ...): never trained on verbatim."""
    from brain_client.expressive.probes import PROBES

    return {p.prompt.split(".")[0].strip().lower() for p in PROBES}


def _rows(directory: Path, source: str, weight: int) -> list[Row]:
    return [
        dict(r, source=source, weight=weight)
        for f in sorted(directory.glob("*.json"))
        for r in json.loads(f.read_text())
    ]


def build(work: Path, out: Path) -> None:
    """dataset.jsonl (accepted, leak-filtered rows), val.jsonl (40 prompts x 2 teacher labels) and report.json."""
    import numpy as np

    rows = _rows(work / "recipes", "astra", 1) + _rows(work / "seed", "seed", 3)
    concepts = probe_concepts()
    leaked = [
        r
        for r in rows
        if OOD_BLOCK.search(f"{r['prompt']} {r['family']}") or r["prompt"].split(".")[0].strip().lower() in concepts
    ]
    accepted = [r for r in rows if r["error"] is None and r not in leaked]
    val_set = val_prompts(work)
    second = {r["prompt"]: r for r in _rows(work / "val2", "astra", 1) if r["error"] is None}
    first = {r["prompt"]: r for r in accepted}
    val = [
        {
            "prompt": p,
            "family": f,
            "labels": [
                {"idea": first[p]["idea"], "recipe": first[p]["recipe"]},
                {"idea": second[p]["idea"], "recipe": second[p]["recipe"]},
            ],
        }
        for p, f in val_set.items()
        if p in first and p in second
    ]
    out.mkdir(parents=True, exist_ok=True)
    keys = ("prompt", "idea", "recipe", "source", "family", "weight")
    (out / "dataset.jsonl").write_text("".join(json.dumps({k: r[k] for k in keys}) + "\n" for r in accepted))
    (out / "val.jsonl").write_text("".join(json.dumps(v) + "\n" for v in val))
    live = [liveliness(r["recipe"]) for r in accepted]
    cats: dict[str, int] = {}
    for r in accepted:
        cats[category(r["family"])] = cats.get(category(r["family"]), 0) + 1
    families_ = {r["family"] for r in accepted}
    report = {
        "rows_written": len(rows),
        "first_pass_valid": sum(r.get("first_error") is None for r in rows) / len(rows),
        "accepted_after_repair": sum(r["error"] is None for r in rows) / len(rows),
        "leak_dropped": len(leaked),
        "dataset_rows": len(accepted),
        "val_prompts": len(val),
        "families": len(families_),
        "median_segments": float(np.median([x.segments for x in live])),
        "median_energy": float(np.median([x.median_energy for x in live])),
        "median_duration_s": float(np.median([x.seconds for x in live])),
        "median_frozen": float(np.median([x.frozen for x in live])),
        "rows_per_category": dict(sorted(cats.items())),
        "rejections": sorted({r["error"].split(":")[0] for r in rows if r["error"]})[:20],
    }
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


def recipes(work: Path, workers: int, per_batch: int, system_file: Path | None, limit: int | None) -> None:
    fams: dict[str, list[str]] = json.loads((work / "families.json").read_text())
    batches = _batches(fams, per_batch)[:limit]
    run_batches(batches, system_prompt(system_file), work / "recipes", workers)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.author", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("families")
    f.add_argument("--work", type=Path, required=True)
    f.add_argument("--workers", type=int, default=8)
    r = sub.add_parser("recipes")
    r.add_argument("--work", type=Path, required=True)
    r.add_argument("--workers", type=int, default=12)
    r.add_argument("--per-batch", type=int, default=14)
    r.add_argument("--limit", type=int, help="only the first N batches (calibration)")
    r.add_argument("--system-file", type=Path, help="the frozen planner prompt as text (default: core's prompt module)")
    sd = sub.add_parser("seed")
    sd.add_argument("--work", type=Path, required=True)
    sd.add_argument("--workers", type=int, default=12)
    sd.add_argument("--system-file", type=Path)
    sd.add_argument(
        "--checker",
        default=f"cd {Path(__file__).parents[1]} && {sys.executable} -m ml.author check",
        help="shell command Codex runs to validate a recipe",
    )
    v = sub.add_parser("val2", help="second independent teacher label for the 40 val prompts")
    v.add_argument("--work", type=Path, required=True)
    v.add_argument("--workers", type=int, default=4)
    v.add_argument("--system-file", type=Path)
    tp = sub.add_parser("probes", help="the teacher's recipes for the probe prompts")
    tp.add_argument("--work", type=Path, required=True)
    tp.add_argument("--samples", type=int, default=12)
    tp.add_argument("--workers", type=int, default=12)
    tp.add_argument("--system-file", type=Path)
    rl = sub.add_parser("real", help="the teacher's recipes for the 12 held-out real emotions")
    rl.add_argument("--work", type=Path, required=True)
    rl.add_argument("--system-file", type=Path)
    b = sub.add_parser("build")
    b.add_argument("--work", type=Path, required=True)
    b.add_argument("--out", type=Path, default=Path(__file__).with_name("distill_data"))
    c = sub.add_parser("check", help="validate one recipe and print its liveliness numbers (Codex's tool)")
    c.add_argument("recipe")
    c.add_argument("--prompt", default="")
    a = ap.parse_args()
    if a.cmd == "families":
        families(a.work, a.workers)
    elif a.cmd == "recipes":
        recipes(a.work, a.workers, a.per_batch, a.system_file, a.limit)
    elif a.cmd == "seed":
        seed(a.work, a.workers, a.system_file, a.checker)
    elif a.cmd == "val2":
        second_labels(a.work, a.workers, a.system_file)
    elif a.cmd == "probes":
        teacher_probes(a.work, a.samples, a.workers, a.system_file)
    elif a.cmd == "real":
        teacher_real(a.work, a.system_file)
    elif a.cmd == "build":
        build(a.work, a.out)
    else:
        error = problem(a.prompt, a.recipe)
        live = liveliness(a.recipe) if not error or "too" in error else None
        print(f"{'OK' if error is None else 'REJECTED: ' + error}")
        if live:
            print(
                f"duration {live.seconds:.2f} s, {live.segments} segments, median E {live.median_energy:.2f}, "
                f"frozen {live.frozen:.0%}"
            )


if __name__ == "__main__":
    sys.exit(main())
