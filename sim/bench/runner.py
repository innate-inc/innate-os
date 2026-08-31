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


def run_episode(
    map_name: str,
    challenge_id: str,
    make_agent,
    max_sim_s: float | None = None,
    render_wh: tuple[int, int] = (160, 120),
) -> Episode:
    """Run one challenge to completion, timeout, or agent exhaustion.

    make_agent(challenge) -> agent, because an auto-planned oracle cannot be
    built until the Challenge object has been loaded, and loading it needs the
    engine that this function creates.
    """
    assets, ch_root = sources()[map_name]
    if assets is not None:
        os.environ["VIRTUAL_MARS_ASSETS"] = str(assets)
    else:
        os.environ.pop("VIRTUAL_MARS_ASSETS", None)

    from mars_sim_driver.challenges import ChallengeEngine
    from mars_sim_driver.core import VirtualMars

    wall0 = time.time()
    blank = Episode(map_name, challenge_id, "?", False, 0, 0, 0.0, "", 0.0, 0)

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
        blank.wall_s = round(time.time() - wall0, 1)
        return blank

    agent = make_agent(ch)
    if agent is None:
        blank.error = "no agent"
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
        blank.error = "engine.start refused"
        blank.wall_s = round(time.time() - wall0, 1)
        return blank

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
    heard = []

    def _deliver(line: dict) -> None:
        heard.append(line)
        hear = getattr(agent, "hear", None)
        if hear is not None:
            hear(line)

    engine.set_cue_sink(_deliver)

    limit = max_sim_s or ch.time_limit_s or 600.0
    t0 = float(mars.data.time)
    steps = 0
    reason = ""

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

    done = sum(1 for g in engine.goal_done if g)
    m = engine.metrics()
    return Episode(
        map=map_name,
        challenge=challenge_id,
        agent=agent.name,
        passed=engine.state == "passed",
        goals_done=done,
        goals_total=len(ch.goals),
        elapsed_s=round(engine.elapsed_s, 1),
        reason=reason or engine.reason,
        wall_s=round(time.time() - wall0, 1),
        steps=steps,
        turns=int(getattr(agent, "turns", 0)),
        camera_errors=int(getattr(agent, "camera_errors", 0)),
        path_len_m=m["path_len_m"],
        goal_times_s=m["goal_times_s"],
        utterances=m["utterances"],
        first_utterance_s=m["first_utterance_s"],
        tempt_min_m=m["tempt_min_m"],
        heard=len(heard),
    )
