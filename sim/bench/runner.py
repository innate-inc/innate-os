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
from dataclasses import dataclass
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


# Episode fields by the primitive they have to be. Anything else is a value
# that survives this process and not the next one.
_TEXT = ("map", "challenge", "agent", "reason", "needs", "error", "blocked")
_WHOLE = (
    "goals_done",
    "goals_total",
    "steps",
    "model_calls",
    "tokens_in",
    "tokens_out",
    "turns",
    "utterances",
    "camera_errors",
    "heard",
)
_REAL = ("elapsed_s", "wall_s", "cost_usd", "path_len_m")
_MAYBE_REAL = ("first_utterance_s", "tempt_min_m")  # None means "never happened"


def _as_primitive(name: str, value):
    """One field, coerced to what the wire can carry. Never raises: a bad
    value must not cost the episode that carries it."""
    if name in _TEXT:
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        try:
            return str(value)
        except BaseException:  # noqa: BLE001 -- a __str__ that raises
            return "<unprintable>"
    if name == "passed":
        try:
            return bool(value)
        except BaseException:  # noqa: BLE001 -- a __bool__ that raises
            return False
    if name in _WHOLE or name in _REAL or name in _MAYBE_REAL:
        if value is None and name in _MAYBE_REAL:
            return None
        cast = int if name in _WHOLE else float
        try:
            return cast(value)
        except BaseException:  # noqa: BLE001
            return None if name in _MAYBE_REAL else cast(0)
    if name == "goal_times_s":
        # A TUPLE, so the guarantee survives the assignment: a list stays
        # mutable, and one append of the wrong thing puts it back on the wire.
        # JSON still writes an array.
        try:
            return tuple(_as_primitive("elapsed_s", t) for t in value)
        except BaseException:  # noqa: BLE001 -- not iterable
            return ()
    return value


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
    # Set once the challenge is actually running. A blocked episode is usually
    # one that never started, but an unreadable score blocks a run that did,
    # and a live episode can reach `running` and be blocked before any
    # measurement moves -- so this is recorded rather than inferred from a step
    # count the live runner never sets.
    started: bool = False
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
    goal_times_s: tuple = ()
    utterances: int = 0
    first_utterance_s: float | None = None
    tempt_min_m: float | None = None  # closest approach to what an ambient cue named
    # Times the camera could not be read. Never silently zero-by-omission: an
    # agent that saw nothing all episode must be distinguishable from one that
    # saw everything and still failed.
    camera_errors: int = 0
    heard: int = 0  # narrator lines delivered this episode

    def __setattr__(self, name: str, value) -> None:
        """Coerce on assignment, so every field is a primitive however it got
        there.

        An Episode crosses a process boundary. A field holding an arbitrary
        object -- a lambda from a buggy agent's `name` or `failed_reason` --
        does not cost that episode: it fails in the pool's result feeder, which
        aborts the sweep. Converting at each return site missed three paths,
        and __post_init__ would still miss the ones that build a blank episode
        and fill it in afterwards.
        """
        object.__setattr__(self, name, _as_primitive(name, value))

    def as_row(self) -> str:
        mark = "BLOK" if self.blocked else ("PASS" if self.passed else "fail")
        if self.blocked:
            # A blocked episode is usually one that never ran, but an
            # unreadable score blocks a run that did -- printing that as "not
            # attempted" would hide the measurements it produced.
            if self.started:
                return (
                    f"{mark:>4}  {self.map:<10} {self.challenge:<28} {self.agent:<7} "
                    f"{self.goals_done}/{self.goals_total}  sim {self.elapsed_s:6.1f}s  "
                    f"not scored -- {self.blocked}"
                )
            return (
                f"{mark:>4}  {self.map:<10} {self.challenge:<28} {self.agent:<7} "
                f"  -/-   not attempted -- {self.blocked}"
            )
        return (
            f"{mark:>4}  {self.map:<10} {self.challenge:<28} {self.agent:<7} "
            f"{self.goals_done}/{self.goals_total}  sim {self.elapsed_s:6.1f}s  "
            f"wall {self.wall_s:5.1f}s  {self.error or self.reason}"
        )


# Who owns Ctrl-C is a policy the CALLER knows, not a fact to be discovered.
# Two previous attempts read it out of the interpreter -- current_process().name
# and then parent_process() -- and both are ordinary mutable state that code
# under test can set, letting an agent have its own KeyboardInterrupt re-raised
# and kill the pool worker. Pool never notices; the parent sees only the result
# timeout and marks unrelated queued episodes blocked.
#
# This is not a security boundary. An agent sharing the interpreter can still
# os._exit() its worker, and the sweep survives that through --result-timeout.
# It closes the accidental path, which is the one that actually happens.
def describe(exc: BaseException) -> str:
    """ "Type: message", without trusting the message to render.

    `f"{exc}"` runs the exception's own __str__, so one that cannot render
    itself raised from inside the handler that had just caught it -- turning a
    guarded failure into an unguarded one that reached the last-resort path and
    was scored against the robot.
    """
    name = type(exc).__name__
    try:
        text = str(exc)
    except BaseException:  # noqa: BLE001
        return f"{name}: <unprintable>"
    return f"{name}: {text}" if text else name


def _opt_float(v):
    """A float, or None. Everything leaving run_episode has to be a primitive:
    a value that is not costs the whole sweep rather than one episode, because
    it fails in the pool's result feeder instead of here."""
    return None if v is None else float(v)


USER_OWNS_INTERRUPT = True
AGENT_OWNS_INTERRUPT = False


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
    user_owns_interrupt: bool = USER_OWNS_INTERRUPT,
) -> Episode:
    """Run one challenge to completion, timeout, or agent exhaustion.

    make_agent(challenge) -> agent, because an auto-planned oracle cannot be
    built until the Challenge object has been loaded, and loading it needs the
    engine that this function creates.

    `user_owns_interrupt` says whether a KeyboardInterrupt reaching this
    function is the terminal's. True for a direct caller, where Ctrl-C is
    control flow and must not become a result. False in a sweep worker, where
    the parent owns the terminal and an interrupt can only have come from the
    code under test -- letting that escape kills the worker.
    """
    wall0 = time.time()
    try:
        ready = _prepare(map_name, challenge_id, make_agent, render_wh, agent_name, wall0)
    except BaseException as exc:  # noqa: BLE001 -- setup, so every failure is ours
        if user_owns_interrupt and isinstance(exc, KeyboardInterrupt):
            raise  # the user asked to stop; that is not a result
        detail = describe(exc)
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

    started = True
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
        if user_owns_interrupt and isinstance(exc, KeyboardInterrupt):
            raise
        crash = describe(exc)
        reason = reason or f"agent crashed: {crash}"

    # Every read below is optional and several of them are the agent's own
    # code -- `turns` is already a property, and `blocked_reason` could be one.
    # A failure here must not discard the episode that produced it: falling
    # through would hand _one an exception and it would fabricate the zeros
    # this guard exists to prevent.
    lost: list[str] = []

    def read(what, fn, default, authoritative=False):
        """Read one field of the finished episode without risking the episode.

        Several of these are the agent's own code -- `turns` is already a
        property -- and a raise here used to escape run_episode entirely, so
        the caller fabricated an all-zero result for a run that had really
        happened. `authoritative` marks the values the SCORE depends on:
        defaulting those would turn an observed pass into a robot failure, so
        instead the episode is blocked and not scored at all.
        """
        nonlocal crash
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001
            if user_owns_interrupt and isinstance(exc, KeyboardInterrupt):
                raise
            note = f"{what} failed: {describe(exc)}"
            crash = f"{crash}; also {note}" if crash else note
            if authoritative:
                lost.append(what)
            return default

    def metrics():
        """Normalise inside the guard: metrics() returning None or a mapping
        missing a key used to raise a KeyError one line later, outside it."""
        got = engine.metrics() or {}
        return {
            "path_len_m": float(got.get("path_len_m") or 0.0),
            "goal_times_s": [float(t) for t in (got.get("goal_times_s") or [])],
            "utterances": int(got.get("utterances") or 0),
            # None is meaningful for both -- never spoke, never approached --
            # so they are converted only when present.
            "first_utterance_s": _opt_float(got.get("first_utterance_s")),
            "tempt_min_m": _opt_float(got.get("tempt_min_m")),
        }

    empty = {
        "path_len_m": 0.0,
        "goal_times_s": [],
        "utterances": 0,
        "first_utterance_s": None,
        "tempt_min_m": None,
    }
    # Read into locals BEFORE building the Episode: arguments evaluate left to
    # right, so passing `error=crash` inline captured it before the reads after
    # it could append, and a finalisation failure went into a variable nobody
    # read again. Every value is converted to a primitive here too -- a
    # non-pickleable agent.name left this function cleanly and then failed in
    # the pool's result feeder, which aborts the whole sweep rather than
    # costing one episode.
    m = read("engine.metrics", metrics, empty)
    name = read("agent.name", lambda: str(agent.name), agent_name)
    passed = read("engine.state", lambda: engine.state == "passed", False, authoritative=True)
    done = read("engine.goal_done", lambda: sum(1 for g in engine.goal_done if g), 0, authoritative=True)
    total = read("challenge goals", lambda: len(ch.goals), 0, authoritative=True)
    elapsed = read("engine.elapsed_s", lambda: round(float(engine.elapsed_s), 1), 0.0)
    why = reason or read("engine.reason", lambda: str(engine.reason), "")
    blocked = read("agent.blocked_reason", lambda: str(getattr(agent, "blocked_reason", "")), "")
    turns = read("agent.turns", lambda: int(getattr(agent, "turns", 0)), 0)
    cameras = read("agent.camera_errors", lambda: int(getattr(agent, "camera_errors", 0)), 0)
    if lost and not blocked:
        # The score cannot be computed from what survived, and a default would
        # be a verdict nobody reached.
        blocked = f"harness: could not read {', '.join(lost)}"
    return Episode(
        map=map_name,
        challenge=challenge_id,
        agent=name,
        passed=passed,
        goals_done=done,
        goals_total=total,
        elapsed_s=elapsed,
        reason=why,
        error=crash,
        blocked=blocked,
        started=started,
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
