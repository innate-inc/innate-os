"""The blind recognition eval end to end, resumable: everything it computes is cached under ``out``.

plan     the planner LLM writes recipes for the held-out and preset prompts (validity, repairs, latency)
probes   the physical probe suite: each probe prompt planned ``samples`` times and checked
clips    every recipe through every arm (lively = procedural liveliness, direct = the bare plan, ...)
media    a key-frame strip (kinematic) and a caption-free mp4 (physical) per clip
judge    ``n`` blind readings per clip, descriptions graded, and ``n`` A/B rounds per prompt and pair of
         arms (``--pair``, lively vs direct by default), each round judged in both orders
report   metrics.json, confusion.png, REPORT.md
"""

from __future__ import annotations

import fnmatch
import json
import logging
import subprocess
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, TypeVar

import _core  # noqa: F401
import numpy as np

from brain_client.expressive import planner, probes
from brain_client.expressive.motion import Clip
from eval.arms import ARMS, Generator, make_clip
from eval.judge import Judge, JudgeKind, Media, Reading, Verdict
from eval.llm import Chat, load_env, openai_chat
from eval.prompts import Item, items
from eval.strips import DEFAULT_CAMERA, Filmstrip, render_blind_video

logger = logging.getLogger("mars-express.eval")
T = TypeVar("T")
R = TypeVar("R")


class Planned(TypedDict):
    model: str
    prompt: str
    idea: str
    recipe: str | None
    attempts: int
    latencies: list[float]
    error: str


@dataclass(frozen=True)
class Config:
    out: Path
    judge: JudgeKind = "gemini"
    judge_model: str | None = None
    n: int = 3
    planner_model: str = "gpt-6-astra"
    samples: int = 8
    arms: Mapping[str, Generator] = field(default_factory=lambda: dict(ARMS))
    pairs: tuple[tuple[str, str], ...] = (("lively", "direct"),)
    workers: int = 8
    render_workers: int = 4
    probes: bool = True
    camera: str = DEFAULT_CAMERA
    only: tuple[str, ...] = ()  # item-id patterns (fnmatch); empty = every item

    @property
    def view(self) -> str:
        """Suffix of the media and judgment directories: the default camera has none, so its cache stands."""
        return "" if self.camera == DEFAULT_CAMERA else f"@{self.camera}"


def _read(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


def _parallel(work: Iterable[T], fn: Callable[[T], R], workers: int, label: str) -> list[tuple[T, R]]:
    """Run ``fn`` over ``work`` in threads; one failed item is logged and skipped, never fatal."""
    todo = list(work)
    done: list[tuple[T, R]] = []
    with ThreadPoolExecutor(workers) as pool:
        futures: dict[Future[R], T] = {pool.submit(fn, item): item for item in todo}
        for k, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                done.append((item, future.result()))
            except Exception as e:  # noqa: BLE001 — one failed API call must not sink the run
                logger.warning("%s %s failed: %s", label, item, e)
            if k % 10 == 0 or k == len(todo):
                logger.info("%s %d/%d", label, k, len(todo))
    return done


def write_recipe(prompt: str, chat: Chat, model: str) -> Planned:
    """``planner.write`` with every chat call timed; a planner that never produces a valid recipe is a result."""
    latencies: list[float] = []

    def timed(messages: list[dict[str, str]]) -> str:
        start = time.monotonic()
        try:
            return chat(messages)
        finally:
            latencies.append(time.monotonic() - start)

    try:
        written = planner.write(prompt, timed, retries=2)
    except planner.PlannerError as e:
        return Planned(model=model, prompt=prompt, idea="", recipe=None, attempts=3, latencies=latencies, error=str(e))
    return Planned(
        model=model,
        prompt=prompt,
        idea=written.idea,
        recipe=written.recipe,
        attempts=written.attempts,
        latencies=latencies,
        error="",
    )


def _stale(planned: Planned | None, model: str, prompt: str) -> bool:
    return planned is None or (planned["model"], planned["prompt"]) != (model, prompt)


def plan(cfg: Config, todo: list[Item]) -> dict[str, Planned]:
    path = cfg.out / "plans.json"
    plans: dict[str, Planned] = _read(path) or {}
    missing = [i for i in todo if i.recipe is None and _stale(plans.get(i.id), cfg.planner_model, i.prompt)]
    chat = openai_chat(cfg.planner_model)
    for item, planned in _parallel(
        missing, lambda i: write_recipe(i.prompt, chat, cfg.planner_model), cfg.workers, "plan"
    ):
        plans[item.id] = planned
        _write(path, plans)
    return plans


def probe_suite(cfg: Config) -> dict[str, list[Planned]]:
    path = cfg.out / "probes.json"
    runs: dict[str, list[Planned]] = _read(path) or {}
    for prompt in list(runs):
        runs[prompt] = [r for r in runs[prompt] if r["model"] == cfg.planner_model]
    work = [(probe.prompt, k) for probe in probes.PROBES for k in range(len(runs.get(probe.prompt, [])), cfg.samples)]
    chat = openai_chat(cfg.planner_model)
    for (prompt, _), planned in _parallel(
        work, lambda w: write_recipe(w[0], chat, cfg.planner_model), cfg.workers, "probe"
    ):
        runs.setdefault(prompt, []).append(planned)
        _write(path, runs)
    return runs


def recipe_of(item: Item, plans: Mapping[str, Planned]) -> str | None:
    return item.recipe if item.recipe is not None else plans.get(item.id, {}).get("recipe")


def seed_of(item: Item) -> int:
    return zlib.crc32(item.id.encode()) & 0xFFFF


def clip_path(cfg: Config, item: Item, arm: str) -> Path:
    return cfg.out / "clips" / f"{item.id}.{arm}.json"


def media_of(cfg: Config, item: Item, arm: str) -> Media:
    strips, videos = cfg.out / f"strips{cfg.view}", cfg.out / f"videos{cfg.view}"
    return Media(strips / f"{item.id}.{arm}.png", videos / f"{item.id}.{arm}.mp4")


def _render_video(job: tuple[str, str, str]) -> str:
    clip_json, mp4, camera = job
    return str(render_blind_video(Clip.load(Path(clip_json)), Path(mp4), camera))


def _changed(path: Path, clip: Clip) -> bool:
    """Whether ``clip`` differs from the one saved at ``path`` beyond the JSON's rounding."""
    if not path.exists():
        return False
    saved = Clip.load(path)
    same = saved.recipe == clip.recipe and saved.frames.shape == clip.frames.shape
    return not (same and np.allclose(saved.frames, clip.frames, atol=2e-4))


def build_media(cfg: Config, todo: list[Item], plans: Mapping[str, Planned]) -> list[tuple[Item, str]]:
    """Clips, strips and videos for every playable (item, arm); returns the playable pairs."""
    playable: list[tuple[Item, str]] = []
    filmstrip = Filmstrip(cfg.camera)
    videos: list[tuple[str, str, str]] = []
    for item in todo:
        recipe = recipe_of(item, plans)
        if recipe is None:
            continue
        idea = plans[item.id]["idea"] if item.id in plans else ""
        for arm, generator in cfg.arms.items():
            clip = make_clip(recipe, generator, seed_of(item), name=item.id, prompt=item.prompt, idea=idea)
            path = clip_path(cfg, item, arm)
            if _changed(path, clip):
                _forget(cfg, item, arm)
            clip.save(path)
            media = media_of(cfg, item, arm)
            if not media.strip.exists():
                filmstrip.render(clip, media.strip)
            if not media.video.exists():
                videos.append((str(path), str(media.video), cfg.camera))
            playable.append((item, arm))
    logger.info("media: %d strips ready, rendering %d videos", len(playable), len(videos))
    with ProcessPoolExecutor(cfg.render_workers) as pool:
        for k, _ in enumerate(pool.map(_render_video, videos), 1):
            if k % 10 == 0 or k == len(videos):
                logger.info("video %d/%d", k, len(videos))
    return playable


def _forget(cfg: Config, item: Item, arm: str) -> None:
    """A clip that changed (a new plan, a retuned generator) takes its media and every judgment with it."""
    stale = [*cfg.out.glob(f"strips*/{item.id}.{arm}.png"), *cfg.out.glob(f"videos*/{item.id}.{arm}.mp4")]
    stale += list((cfg.out / "judged").glob(f"*/*/{item.id}.{arm}.json"))
    stale += [
        path
        for path in (cfg.out / "judged").glob(f"*/ab/{item.id}.*.json")
        if arm in path.stem.split(".")[-1].split("-")
    ]
    for path in stale:
        path.unlink(missing_ok=True)


def ab_path(root: Path, item: Item, pair: tuple[str, str]) -> Path:
    """One A/B file per prompt and pair of arms: rounds of [first arm shown first, second arm shown first]."""
    return root / "ab" / f"{item.id}.{pair[0]}-{pair[1]}.json"


def judge_tag(judge: Judge) -> str:
    return f"{judge.kind}-{judge.model}"


def judge_all(cfg: Config, playable: list[tuple[Item, str]]) -> None:
    judge = Judge(cfg.judge, cfg.judge_model)
    root = cfg.out / "judged" / f"{judge_tag(judge)}{cfg.view}"

    def readings(job: tuple[Item, str]) -> None:
        item, arm = job
        path = root / "readings" / f"{item.id}.{arm}.json"
        done: list[Reading] = _read(path) or []
        while len(done) < cfg.n:
            done.append(judge.read(media_of(cfg, item, arm)))
            _write(path, done)

    def grades(job: tuple[Item, str]) -> None:
        item, arm = job
        path = root / "grades" / f"{item.id}.{arm}.json"
        done: list[Reading] = _read(root / "readings" / f"{item.id}.{arm}.json") or []
        scores: list[int] = _read(path) or []
        for reading in done[len(scores) :]:
            scores.append(judge.grade(item.prompt, reading["description"]))
            _write(path, scores)

    def pairs(job: tuple[Item, tuple[str, str]]) -> None:
        item, (a, b) = job
        path = ab_path(root, item, (a, b))
        rounds: list[list[Verdict]] = _read(path) or []
        shown = ((a, media_of(cfg, item, a)), (b, media_of(cfg, item, b)))
        while len(rounds) < cfg.n:
            rounds.append([judge.pair(*shown), judge.pair(*shown[::-1])])
            _write(path, rounds)

    arms_of: dict[str, set[str]] = {}
    for item, arm in playable:
        arms_of.setdefault(item.id, set()).add(arm)
    matches = [
        (item, pair)
        for item in {i.id: i for i, _ in playable}.values()
        for pair in cfg.pairs
        if set(pair) <= arms_of[item.id]
    ]

    _parallel(playable, readings, cfg.workers, "read")
    _parallel(playable, grades, cfg.workers, "grade")
    _parallel(matches, pairs, cfg.workers, "pair")


def core_version() -> str:
    """The last commit that touched the core library, and whether it has uncommitted edits."""
    repo, core = Path(__file__).resolve().parents[2], "ros2_ws/src/brain/brain_client/brain_client/expressive"

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args, "--", core], capture_output=True, text=True
        ).stdout.strip()

    return git("log", "-1", "--format=%h %s") + (" + uncommitted edits" if git("status", "--porcelain") else "")


def run(cfg: Config, report_only: bool = False, snapshot: Path | None = None) -> Path:
    from eval.report import write_report

    load_env()
    todo = [i for i in items() if not cfg.only or any(fnmatch.fnmatch(i.id, pattern) for pattern in cfg.only)]
    if not report_only:
        plans = plan(cfg, todo)
        if cfg.probes:
            probe_suite(cfg)
        playable = build_media(cfg, todo, plans)
        _write(cfg.out / "core.json", {"core": core_version(), "at": time.strftime("%Y-%m-%d %H:%M")})
        judge_all(cfg, playable)
    return write_report(cfg.out, snapshot)
