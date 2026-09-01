"""One benchmark episode: build the world, start a challenge, drive an agent,
judge with the real ChallengeEngine.

No ROS. VirtualMars plus ChallengeEngine directly, which is what makes running
these in parallel at faster-than-real-time possible at all.

The engine expects the world-server thread model (physics thread ticking,
observer threads commanding). Headless there is exactly one thread, so the sim
lock is uncontended -- but it is still passed and still taken, because start()
takes it internally and reaching in without it would be a lie that breaks the
moment anything here goes concurrent.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))

# Physics runs at 2 ms; controlling every step is wasted work for a base that
# accelerates over tenths of a second. 20 Hz control, 10 Hz judging.
CONTROL_DT = 0.05
JUDGE_EVERY = 2
# Longest a single model call may take before the episode is abandoned. Twice
# the backends' own subprocess timeout, so a backend that enforces its own
# limit reports the error itself and this only catches one that has wedged.
THINK_WALL_CAP_S = 360.0

# "apartment" is the stock world: no bundle, challenges from the tracked dir.
APARTMENT = "apartment"


def sources() -> dict[str, tuple[Path | None, Path]]:
    """{name: (assets_dir_or_None, challenges_root)} for every challenge set."""
    out: dict[str, tuple[Path | None, Path]] = {APARTMENT: (None, REPO / "sim" / "challenges")}
    for d in sorted((REPO / "sim" / "bundles").glob("*")):
        if (d / "challenges").is_dir():
            out[d.name] = (d, d / "challenges")
    return out


@dataclass
class Episode:
    map: str
    challenge: str
    agent: str
    passed: bool
    goals_done: int
    goals_total: int
    elapsed_s: float
    reason: str
    wall_s: float
    steps: int
    needs: str = ""
    error: str = ""
    # Set when the deployment cannot perform something the challenge requires
    # (today: no INNATE_SERVICE_KEY, so pick_any_object fails on its first
    # line). A blocked challenge is NOT a failure and must never be counted as
    # one -- nineteen confident zeros from a capability that was never wired up
    # read as an agent that cannot follow instructions. See capabilities.py.
    blocked: str = ""
    # Filled on the LIVE path by live_runner's probe and usage attribution.
    # The in-process runner leaves them at zero (it fills turns/path_len_m
    # itself); the live path could not, so every live failure looked the same
    # in the results file -- a robot that never moved and one that drove into a
    # wall both read as "0/2, timeout".
    model_calls: int = 0  # generate calls billed inside this episode
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    # -- measured, not judged. A pass/fail hides the difference between a robot
    # -- that drove straight there and one that took four minutes and three
    # -- wrong rooms; these are what "where does it break" is actually read off.
    turns: int = 0  # agent decisions taken (its own count)
    path_len_m: float = 0.0  # distance actually driven, integrated
    goal_times_s: list = field(default_factory=list)
    utterances: int = 0
    first_utterance_s: float | None = None
    tempt_min_m: float | None = None  # closest approach to what an ambient cue named
    # Times the camera could not be read. Never silently zero-by-omission: an
    # agent that saw nothing all episode must be distinguishable from one that
    # saw everything and still failed.
    camera_errors: int = 0
    heard: int = 0  # narrator lines delivered this episode

    def as_row(self) -> str:
        mark = "BLOK" if self.blocked else ("PASS" if self.passed else "fail")
        if self.blocked:
            return (
                f"{mark:>4}  {self.map:<10} {self.challenge:<28} {self.agent:<7} "
                f"  -/-   not attempted -- {self.blocked}"
            )
        return (
            f"{mark:>4}  {self.map:<10} {self.challenge:<28} {self.agent:<7} "
            f"{self.goals_done}/{self.goals_total}  sim {self.elapsed_s:6.1f}s  "
            f"wall {self.wall_s:5.1f}s  {self.error or self.reason}"
        )


def _user_interrupt(exc: BaseException) -> bool:
    """Is this a Ctrl-C from the terminal, rather than from the code under test?

    Only the root process has no parent, and only its main thread is where the
    terminal's interrupt is delivered. Sweep workers install their own SIGINT
    handler, so a KeyboardInterrupt raised inside one came from the agent -- and
    letting that escape kills the worker, which Pool never notices and the
    parent can only see as a timeout.

    Deliberately NOT `current_process().name == "MainProcess"`: that name is an
    ordinary mutable attribute, and code running in the worker can set it.
    """
    import multiprocessing as mp
    import threading

    return (
        isinstance(exc, KeyboardInterrupt)
        and mp.parent_process() is None
        and threading.current_thread() is threading.main_thread()
    )


class _Ready(NamedTuple):
    """What the run needs once setup has succeeded."""

    mars: object
    sim_lock: threading.Lock
    engine: object
    ch: object
    agent: object
    nav: object


def _prepare(map_name, challenge_id, make_agent, render_wh, agent_name, wall0):
    """Build the world, the judge and the agent, and start the challenge.

    Everything here is the harness's own work: the robot has not been asked
    anything yet, so a failure is ours and the episode must not be scored.
    Returns a finished (blocked) Episode when it cannot proceed, otherwise a
    _Ready for the run to use.
    """
    assets, ch_root = sources()[map_name]
    if assets is not None:
        os.environ["VIRTUAL_MARS_ASSETS"] = str(assets)
    else:
        os.environ.pop("VIRTUAL_MARS_ASSETS", None)

    from mars_sim_driver import core as _core
    from mars_sim_driver import world as _world
    from mars_sim_driver.challenges import ChallengeEngine
    from mars_sim_driver.core import VirtualMars

    # core.ASSETS_DIR is read once at import. If something imported the sim
    # before this ran, the line above changed the environment and nothing else,
    # and the world would be built without this bundle's props.
    _core.ASSETS_DIR = _world.default_assets_dir()

    wall0 = time.time()
    # Named up front: a challenge that is not under its root fails before any
    # agent is built, and reporting that as "?" loses the one thing the reader
    # needs -- which agent was asked.
    blank = Episode(map_name, challenge_id, agent_name, False, 0, 0, 0.0, "", 0.0, 0)

    # Renders are the expensive part of a headless episode and nothing here
    # looks at pixels, so keep the offscreen buffers small.
    # 160x120 for agents that never look -- renders are the expensive part of a
    # headless episode. But the real camera is 640x480, and an agent scored on
    # perception at a sixteenth of the pixels is being scored on the harness.
    mars = VirtualMars(render_wh=render_wh)
    sim_lock = threading.Lock()

    # Progress is per-episode and thrown away: the shared workspace/challenges.json
    # is a user's record, and parallel workers would race each other writing it.
    progress = (
        Path(__file__).resolve().parent / "results" / "progress" / f"{map_name}_{challenge_id}_{os.getpid()}.json"
    )
    engine = ChallengeEngine(mars, sim_lock, roots=[ch_root], progress_path=progress)

    ch = engine.challenges.get(challenge_id)
    if ch is None:
        blank.error = f"challenge {challenge_id!r} not under {ch_root}"
        blank.blocked = f"harness: {blank.error}"
        blank.wall_s = round(time.time() - wall0, 1)
        return blank

    # Any failure in here is setup, so it needs no special exception type:
    # run_episode blocks everything _prepare raises.
    agent = make_agent(ch)
    if agent is None:
        blank.error = "no agent"
        blank.blocked = "harness: no agent could be built"
        return blank

    blank.agent = agent.name
    if hasattr(agent, "frame_dir"):
        agent.frame_dir = (
            Path(__file__).resolve().parent / "results" / "frames" / agent.name.replace(":", "_") / challenge_id
        )

    # Nav map BEFORE start(): props are still parked off-map, so the grid holds
    # only static geometry. Built after the drops, every target prop rasterises
    # as an obstacle and the planner cannot route to the thing it is meant to
    # approach.
    nav = None
    if hasattr(agent, "nav"):
        from navplan import NavMap

        nav = NavMap.from_sim(mars)

    if not engine.start(challenge_id):
        blank.agent = agent.name
        blank.goals_total = len(ch.goals)
        # The judge knows why it refused -- a predicate whose reset raised,
        # say -- and that reason is the finding. Reporting a bare refusal here
        # turned a bug in the judge into a robot that failed the task.
        blank.error = f"engine.start refused: {engine.reason}" if engine.reason else "engine.start refused"
        blank.blocked = f"harness: {blank.error}"
        blank.wall_s = round(time.time() - wall0, 1)
        return blank

    return _Ready(mars, sim_lock, engine, ch, agent, nav)


def run_episode(
    map_name: str,
    challenge_id: str,
    make_agent,
    max_sim_s: float | None = None,
    render_wh: tuple[int, int] = (160, 120),
    agent_name: str = "?",
) -> Episode:
    """Run one challenge to completion, timeout, or agent exhaustion.

    make_agent(challenge) -> agent, because an auto-planned oracle cannot be
    built until the Challenge object has been loaded, and loading it needs the
    engine that this function creates.
    """
    wall0 = time.time()
    try:
        ready = _prepare(map_name, challenge_id, make_agent, render_wh, agent_name, wall0)
    except BaseException as exc:  # noqa: BLE001 -- setup, so every failure is ours
        if _user_interrupt(exc):
            raise  # the user asked to stop; that is not a result
        detail = f"{type(exc).__name__}: {exc}"
        return Episode(
            map_name,
            challenge_id,
            agent_name,
            False,
            0,
            0,
            0.0,
            "",
            round(time.time() - wall0, 1),
            0,
            error=detail,
            blocked=f"harness: setup failed ({detail[:100]})",
        )
    if isinstance(ready, Episode):
        return ready
    mars, sim_lock, engine, ch, agent, nav = ready

    # Past here it is the run, and the run is the agent's. A crash below is a
    # failed challenge, not a harness fault -- and the goals, time and distance
    # measured up to it are real, so the episode is finalised from the engine
    # rather than rebuilt from zeros.
    # Initialised out here because the finalisation below reads them whether
    # the run completed or crashed -- including a crash in agent.reset, before
    # the loop has assigned anything.
    crash = ""
    reason = ""
    steps = 0
    heard: list[dict] = []
    try:
        if nav is not None:
            agent.reset(mars, ch, nav=nav)
        else:
            agent.reset(mars, ch)
        # Agents that answer questions rather than move need a way to say so.
        if hasattr(agent, "bind_events"):
            agent.bind_events(engine.post_event)

        # The narrator speaks INTO the agent. An agent with no ear still runs --
        # the engine keeps the transcript and fires the cues either way -- which is
        # what lets a deaf oracle gate a scripted challenge for solvability while
        # the scripted content is only scored against agents that can hear.

        def _deliver(line: dict) -> None:
            heard.append(line)
            hear = getattr(agent, "hear", None)
            if hear is not None:
                hear(line)

        engine.set_cue_sink(_deliver)

        limit = max_sim_s or ch.time_limit_s or 600.0
        t0 = float(mars.data.time)

        # Sim time is the currency every time limit is
        # denominated in, and headless the sim runs ~10x real time -- so without
        # this, one second of model latency costs the agent ten seconds of world.
        _think_budget = {"wall0": None, "sim0": None}

        while True:
            agent.act(mars, float(mars.data.time) - t0)

            if getattr(agent, "thinking", False):
                if _think_budget["wall0"] is None:
                    _think_budget["wall0"] = time.time()
                    _think_budget["sim0"] = float(mars.data.time)
                spent_wall = time.time() - _think_budget["wall0"]
                spent_sim = float(mars.data.time) - _think_budget["sim0"]
                # A backend may declare a NOMINAL per-call think charge. The 1:1
                # wall rule is right when the call latency IS the model's latency;
                # for the file-bridge probe the wall time is mostly orchestration
                # (a subagent polling files), and charging it measures the
                # plumbing, not the robot: one 295 s deliberation ate 70% of a
                # 420 s challenge that the agent was actually solving. With
                # think_charge_s set, each call advances sim by at most that many
                # seconds -- a realistic strong-model latency -- however long the
                # call really takes.
                charge = getattr(getattr(agent, "backend", None), "think_charge_s", None)
                wall_cap = getattr(getattr(agent, "backend", None), "think_wall_cap_s", THINK_WALL_CAP_S)
                # A hung backend would otherwise spin here forever: sim time is
                # pinned to the wall clock while thinking, so the challenge time
                # limit -- which is denominated in SIM seconds -- can never fire.
                # The loop would hold a worker until something outside killed it.
                if spent_wall > wall_cap:
                    reason = f"agent stalled: {spent_wall:.0f}s in one model call"
                    break
                if spent_sim >= (spent_wall if charge is None else min(spent_wall, charge)):
                    # The world has kept pace with the thinking. Yield rather than
                    # spin: the model call is on another thread and wants the CPU
                    # far more than this loop does.
                    time.sleep(0.002)
                    continue
            else:
                _think_budget["wall0"] = None

            mars.step(CONTROL_DT)
            steps += 1

            if steps % JUDGE_EVERY == 0:
                with sim_lock:
                    t = float(mars.data.time)
                    pose = mars.pose()
                    centers = mars.object_centers()
                    epoch = engine.world_epoch
                engine.tick(t, pose, centers, epoch)

                if engine.state != "running":
                    break
                if t - t0 > limit:
                    reason = "time limit"
                    break
                # An agent out of plan will never do anything else; burning the
                # remaining sim time proves nothing and costs minutes across a sweep.
                if getattr(agent, "done", False):
                    reason = getattr(agent, "failed_reason", "") or "agent finished its plan"
                    break

    except BaseException as exc:  # noqa: BLE001
        # Including KeyboardInterrupt and SystemExit: raised HERE they came
        # from the code under test, and letting one escape kills the pool
        # worker, which the parent can only see as a timeout -- so unrelated
        # queued episodes get marked blocked too. A real Ctrl-C in the parent
        # is not this: sweep workers ignore SIGINT (main.py), so in a worker
        # only the agent can produce one, and in the main process it is the
        # user and must not be swallowed.
        if _user_interrupt(exc):
            raise
        crash = f"{type(exc).__name__}: {exc}"
        reason = reason or f"agent crashed: {crash}"

    # Every read below is optional and several of them are the agent's own
    # code -- `turns` is already a property, and `blocked_reason` could be one.
    # A failure here must not discard the episode that produced it: falling
    # through would hand _one an exception and it would fabricate the zeros
    # this guard exists to prevent.
    def read(what, fn, default):
        nonlocal crash
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001
            if _user_interrupt(exc):
                raise
            note = f"{what} failed: {type(exc).__name__}: {exc}"
            crash = f"{crash}; also {note}" if crash else note
            return default

    empty = {
        "path_len_m": 0.0,
        "goal_times_s": [],
        "utterances": 0,
        "first_utterance_s": None,
        "tempt_min_m": None,
    }
    # Read everything BEFORE building the Episode: arguments are evaluated left
    # to right, so passing `error=crash` inline captured the value before the
    # reads after it could append to it, and a finalisation failure went into a
    # variable nobody looked at again.
    m = read("engine.metrics", engine.metrics, empty)
    name = read("agent.name", lambda: agent.name, agent_name)
    passed = read("engine.state", lambda: engine.state == "passed", False)
    done = read("engine.goal_done", lambda: sum(1 for g in engine.goal_done if g), 0)
    elapsed = read("engine.elapsed_s", lambda: round(engine.elapsed_s, 1), 0.0)
    why = reason or read("engine.reason", lambda: engine.reason, "")
    blocked = read("agent.blocked_reason", lambda: str(getattr(agent, "blocked_reason", "")), "")
    turns = read("agent.turns", lambda: int(getattr(agent, "turns", 0)), 0)
    cameras = read("agent.camera_errors", lambda: int(getattr(agent, "camera_errors", 0)), 0)
    return Episode(
        map=map_name,
        challenge=challenge_id,
        agent=name,
        passed=passed,
        goals_done=done,
        goals_total=len(ch.goals),
        elapsed_s=elapsed,
        reason=why,
        error=crash,
        blocked=blocked,
        wall_s=round(time.time() - wall0, 1),
        steps=steps,
        turns=turns,
        camera_errors=cameras,
        path_len_m=m["path_len_m"],
        goal_times_s=m["goal_times_s"],
        utterances=m["utterances"],
        first_utterance_s=m["first_utterance_s"],
        tempt_min_m=m["tempt_min_m"],
        heard=len(heard),
    )
