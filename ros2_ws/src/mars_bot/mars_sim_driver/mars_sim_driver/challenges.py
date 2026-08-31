"""Sim challenge engine: declarative tasks verified against ground truth.

A challenge is a Python module in sim/challenges/ exporting CHALLENGE =
Challenge(...) -- the same sidecar shape as the props it drops (props.py): a
scene setup (which props to drop where), an ordered goal checklist of
predicates over ground-truth world state and robot skill events, and an
optional time limit. The engine is hosted by the world server, which owns
ground truth -- the robot stack never sees any of this (that is what makes the
verification honest):

- tick() runs on every observer state broadcast and returns the "challenge"
  block embedded in the state stream, so any frontend is a thin renderer;
- start/abort arrive as observer-channel commands (like drop_prop_at);
- results persist to workspace/challenges.json across restarts;
- skill executions stream in from rosbridge (/brain/skill_status_update via
  SkillEventBridge) -- best-effort: with rosbridge down, world-state
  challenges still work and SkillDone goals simply never fire.

Everything challenge-side is wrapped so a broken challenge file or predicate
degrades that challenge, never the sim.

Example (sim/challenges/shepherd.py):

    from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

    CHALLENGE = Challenge(
        id="shepherd",
        title="Shepherd",
        brief="Find the soccer ball and push it to the dog.",
        setup=[Drop("labrador", 2.1, -3.4, yaw_deg=90), Drop("soccer_ball", -4.5, -1.2)],
        goals=[
            Goal("Find the ball", Near("robot", "soccer_ball", 0.8)),
            Goal("Push it to the dog", Near("soccer_ball", "labrador", 1.0)),
        ],
        time_limit_s=300,
    )
"""

import hashlib
import importlib.util
import json
import math
import re
import socket
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from . import world

# How far sim time may run backwards before tick() reads it as the world
# having been rebuilt under the run. Two publish_state callers (the physics
# thread and an observer command) can legitimately deliver snapshots about one
# physics slice apart, ~25ms; a reset drops the clock by the whole uptime.
CLOCK_REWIND_S = 0.5

# Bound the final websocket send so Abort/Start cannot wait indefinitely.
CHAT_WRITE_TIMEOUT_S = 2.0
CHAT_OUTBOX_MAXLEN = 64

# Sender the brain routes to a voice instead of straight into the chat.
ENVIRONMENT_SPEECH_SENDER = "environment_speech"


class _ChatWriteTimeout(TimeoutError):
    """A bounded chat write hit its deadline -- delivery is unknown."""


# Robot-owned JSON cannot reproduce this in-process provenance marker.
_ENVIRONMENT_EVENT_SOURCE = object()

# --- world-state view handed to predicates ---


@dataclass
class WorldState:
    """Ground truth at one tick. Predicates read positions via pos()."""

    t: float
    robot: tuple[float, float, float]  # x, y, yaw
    # Prop name -> xy of its visual CENTRE (PropRegistry.center_xy, which
    # applies the sidecar's center_offset: the human scan stands feet-at-
    # origin, and a Near() against it must not measure to the feet of a 1.7m
    # body). Props still parked off-map are absent, not reported.
    centers: dict[str, tuple[float, float]]
    # Seconds since THIS RUN started, not absolute sim time. A challenge
    # author cannot use absolute time for anything: the world server has been
    # up for an hour and the run began ninety seconds ago. Defaulted so every
    # existing construction site keeps working.
    elapsed: float = 0.0

    # {prop: z} for props on the floor, filled by the engine from the sim it
    # already holds. Defaulted so every existing construction site keeps
    # working and so an in-process caller that does not supply it simply gets
    # goals that cannot assert height.
    heights: dict[str, float] = field(default_factory=dict)

    def pos(self, name: str) -> tuple[float, float] | None:
        """xy of "robot" or a prop; None while that prop isn't dropped."""
        if name == "robot":
            return self.robot[0], self.robot[1]
        return self.centers.get(name)

    def height(self, name: str) -> float | None:
        """z of a prop, or None when unknown -- which must never be read as
        "too low": a goal asserting min_z passes on None so a missing height
        cannot silently fail a challenge that used to pass."""
        if name == "robot":
            return None
        return self.heights.get(name)


# --- predicates (each: update(state, events) -> bool, reset() for reuse) ---


class Predicate:
    def observe(self, state: "WorldState", events: list[dict]) -> None:
        """Offered every event batch, judged or not. Default: ignore.

        Goals are ordered, so a predicate is only UPDATED on its turn. A goal
        that asserts something about the whole run ("and never claimed it
        couldn't") has to see the events that happened while earlier goals
        were still open, or it is judging an empty list.
        """
        return None

    def update(self, state: WorldState, events: list[dict]) -> bool:
        raise NotImplementedError

    def reset(self) -> None:
        pass


@dataclass
class Near(Predicate):
    """xy distance between two things ("robot" or a prop kind) <= radius."""

    a: str
    b: str
    radius_m: float

    def update(self, state: WorldState, events: list[dict]) -> bool:
        pa, pb = state.pos(self.a), state.pos(self.b)
        if pa is None or pb is None:
            return False
        return math.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= self.radius_m


@dataclass
class InCircle(Predicate):
    """A thing is within radius of a fixed world point (a named spot)."""

    target: str
    x: float
    y: float
    radius_m: float
    # Optional floor for the object's height. "On the pass" is a different
    # claim from "within 45 cm of the middle of the pass", and without this the
    # second was standing in for the first -- see sim/bench/FINDINGS.md
    # (patch_goal_height).
    min_z: float | None = None

    def update(self, state: WorldState, events: list[dict]) -> bool:
        p = state.pos(self.target)
        if p is None or math.hypot(p[0] - self.x, p[1] - self.y) > self.radius_m:
            return False
        if self.min_z is None:
            return True
        z = state.height(self.target)
        # Unknown height FAILS. min_z is what separates "on the counter" from
        # "in the counter's footprint, on the floor"; awarding it when the
        # height cannot be read credits a goal the robot may not have reached.
        return z is not None and z >= self.min_z


@dataclass
class InRect(Predicate):
    """A thing is inside an axis-aligned world rectangle (a room, a zone)."""

    target: str
    x0: float
    y0: float
    x1: float
    y1: float
    # Same contract as InCircle.min_z (see sim/bench/FINDINGS.md,
    # patch_goal_height): "on the counter" is a different claim from "within
    # the counter's footprint", and unknown height PASSES so a missing z can
    # never fail a goal.
    min_z: float | None = None

    def update(self, state: WorldState, events: list[dict]) -> bool:
        p = state.pos(self.target)
        if p is None:
            return False
        if not (
            min(self.x0, self.x1) <= p[0] <= max(self.x0, self.x1)
            and min(self.y0, self.y1) <= p[1] <= max(self.y0, self.y1)
        ):
            return False
        if self.min_z is None:
            return True
        z = state.height(self.target)
        return z is not None and z >= self.min_z  # unknown height fails; see InCircle


@dataclass
class Hold(Predicate):
    """Inner predicate continuously true for `seconds` of sim time (dwell)."""

    inner: Predicate
    seconds: float
    _since: float | None = field(default=None, repr=False)

    def update(self, state: WorldState, events: list[dict]) -> bool:
        if not self.inner.update(state, events):
            self._since = None
            return False
        if self._since is None:
            self._since = state.t
        return state.t - self._since >= self.seconds

    def observe(self, state: WorldState, events: list[dict]) -> None:
        self.inner.observe(state, events)

    def reset(self) -> None:
        self._since = None
        self.inner.reset()


@dataclass
class SkillDone(Predicate):
    """The robot completed a skill (matched against /brain/skill_status_update
    by skill_id or display name). An optional guard predicate must hold at the
    moment the completion event arrives ("bark WHILE near the dog")."""

    skill: str
    guard: Predicate | None = None

    def update(self, state: WorldState, events: list[dict]) -> bool:
        for ev in events:
            if ev.get("status") != "completed":
                continue
            if self.skill not in (ev.get("skill_id"), ev.get("skill_name")):
                continue
            if self.guard is None or self.guard.update(state, events):
                return True
        return False

    def reset(self) -> None:
        if self.guard is not None:
            self.guard.reset()


@dataclass
class Answered(Predicate):
    """The robot reported an ANSWER, and it was right.

    Every other predicate here judges where things are. A whole class of task
    -- "turn around and count the items", "which room is the mug in?" -- has no
    position that settles it: the robot can be in exactly the right place and
    still be wrong. Without this the observation-and-conversation category
    cannot be scored at all, only approximated by driving somewhere.

    Matches events of the shape {"type": "answer", "value": ...} posted through
    ChallengeEngine.post_event, alongside the skill_status_update events
    SkillDone reads. Values compare as strings, case- and space-insensitively,
    so "4", 4 and " Four" are not gratuitously different answers; `accept`
    carries every spelling that counts as correct.
    """

    accept: list[str]
    key: str = "value"

    @staticmethod
    def _norm(v) -> str:
        return str(v).strip().lower()

    def _says_it(self, text: str) -> bool:
        """An accepted spelling appears in free speech as a whole word.

        Whole-word, so "3" does not match "30" and "four" does not match
        "fourteen". See sim/bench/FINDINGS.md (patch_answer) for the known
        weakness: a hedge that contains the right token passes.
        """
        low = f" {text.strip().lower()} "
        for a in self.accept:
            token = str(a).strip().lower()
            if not token:
                continue
            if re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", low):
                return True
        return False

    def update(self, state: WorldState, events: list[dict]) -> bool:
        wanted = {self._norm(a) for a in self.accept}
        for ev in events:
            kind = ev.get("type")
            if kind == "answer":
                # The structured channel: the whole value must be the answer.
                if self._norm(ev.get(self.key)) in wanted:
                    return True
                # An agent that put a sentence in the answer channel still
                # answered. Fall through to the same text rule as speech.
                if self._says_it(str(ev.get(self.key, ""))):
                    return True
            elif kind == "say":
                # Speech. From the robot's side this IS answering, and which
                # envelope a stack uses is its own business.
                if self._says_it(str(ev.get("text", ""))):
                    return True
        return False


@dataclass
class EventSeen(Predicate):
    """Latch once a typed challenge event with the requested fields arrives.

    Unlike :class:`SkillDone`, this is for environment-owned events rather
    than robot skill lifecycle updates. The latch lets several EventSeen
    predicates inside an AllOf complete on different ticks and in any order.
    """

    type: str
    fields: dict[str, object] = field(default_factory=dict)
    guard: Predicate | None = None
    _seen: bool = field(default=False, init=False, repr=False)

    def update(self, state: WorldState, events: list[dict]) -> bool:
        if self._seen:
            return True
        for event in events:
            if event.get("_source") is not _ENVIRONMENT_EVENT_SOURCE:
                continue
            if event.get("type") != self.type:
                continue
            if any(event.get(key) != value for key, value in self.fields.items()):
                continue
            if self.guard is None or self.guard.update(state, events):
                self._seen = True
                break
        return self._seen

    def reset(self) -> None:
        self._seen = False
        if self.guard is not None:
            self.guard.reset()


# Every child is updated on every tick, never short-circuited: a stateful
# predicate only advances when it is asked, so a Hold sitting behind a decided
# sibling would silently restart its dwell each tick if all()/any() stopped
# early. Judge first, combine second.


@dataclass
class Said(Predicate):
    """The robot said something matching one of these patterns.

    Answered() is for questions with an answer key -- a count, a colour, a
    room. A whole category of conversational behaviour has no answer key:
    admitting a limit ("I can't reach the top shelf"), asking which of two
    things was meant, acknowledging a correction. Scoring those against a
    fixed accept-list would score the phrasing rather than the behaviour, so
    this matches case-insensitive regexes against whatever the robot uttered.

    Reads BOTH channels -- {"type": "say"} and the {"type": "answer"} that
    Answered watches -- because from the robot's side they are one act. Which
    envelope a given stack happens to use is an implementation detail of that
    stack, and a benchmark that only listened to one would score the wiring.

    Patterns are deliberately regexes and not substrings: "can'?t|cannot|
    unable" is one alternation, whereas the substring version needs three
    entries and still misses "can not".
    """

    patterns: list[str]
    negate: bool = False  # True: the goal is that the robot did NOT say this
    _violated: bool = field(default=False, repr=False)
    # One utterance that satisfies `patterns`, for the oracle to speak. A regex
    # cannot be run backwards into a sentence, so without this the validity
    # gate could never show that a speech goal is satisfiable AT ALL -- and an
    # ungateable goal is exactly the kind of number this suite refuses to
    # report. Authoring it also forces the author to check their own regex.
    oracle_line: str = ""

    def _hit(self, events: list[dict]) -> bool:
        for ev in events:
            if ev.get("type") not in ("say", "answer"):
                continue
            text = str(ev.get("text", ev.get("value", "")))
            if any(re.search(p, text, re.IGNORECASE) for p in self.patterns):
                return True
        return False

    def observe(self, state: WorldState, events: list[dict]) -> None:
        # Only the negative form needs a memory. The positive form is "say this
        # once you get here", and hearing it early -- before the goal that puts
        # the robot in front of the thing it is talking about -- should not
        # count, so it deliberately does not accumulate.
        if self.negate and self._hit(events):
            self._violated = True

    def update(self, state: WorldState, events: list[dict]) -> bool:
        if self.negate:
            # Latches true the first time it is judged, UNLESS a violating
            # utterance was heard at any point in the run so far. Ordered goals
            # mean this is judged only after everything before it is done, so
            # "and never said it along the way" covers the whole way.
            return not self._violated
        return self._hit(events)

    def reset(self) -> None:
        self._violated = False


@dataclass
class After(Predicate):
    """inner, but only once the run is at least  old.

    The building block for a world that changes on its own. Put one in
    fail_if and a region becomes lethal at a known moment -- a room the fire
    has reached, a door that locks -- and the agent's problem stops being
    "can I get there" and becomes "what do I give up".

    Deliberately NOT the inverse of Hold. Hold asks that something stay true
    for a duration; this asks that the clock have passed a mark. An author who
    wants "in the kitchen after 90 seconds" wants this; expressing it with
    Hold would mean "in the kitchen FOR 90 seconds", which a robot passing
    through never satisfies.
    """

    seconds: float
    inner: Predicate

    def update(self, state: WorldState, events: list[dict]) -> bool:
        if state.elapsed < self.seconds:
            # The inner predicate is still offered every tick via observe(),
            # so anything with a memory keeps it. Only the verdict is gated.
            return False
        return self.inner.update(state, events)

    def observe(self, state: WorldState, events: list[dict]) -> None:
        self.inner.observe(state, events)

    def reset(self) -> None:
        self.inner.reset()


class _Composite(Predicate):
    """Shared observe/reset for the combinators, so a Said nested inside an
    AllOf still sees the run. Forgetting to forward observe() would make a
    negated goal silently unfalsifiable, which is the exact failure this
    mechanism exists to remove."""

    def observe(self, state: "WorldState", events: list[dict]) -> None:
        for child in self.preds:
            child.observe(state, events)


@dataclass
class AllOf(_Composite):
    preds: list[Predicate]

    def update(self, state: WorldState, events: list[dict]) -> bool:
        return all([p.update(state, events) for p in self.preds])  # noqa: C419 -- see above

    def reset(self) -> None:
        for p in self.preds:
            p.reset()


@dataclass
class AnyOf(_Composite):
    preds: list[Predicate]

    def update(self, state: WorldState, events: list[dict]) -> bool:
        return any([p.update(state, events) for p in self.preds])  # noqa: C419 -- see above

    def reset(self) -> None:
        for p in self.preds:
            p.reset()


# --- challenge definition ---


@dataclass
class Drop:
    """Scene setup: drop a prop (a sidecar in sim/props/, by its `name`) at
    (x, y) when the challenge starts; physics settles it onto whatever is
    below. A name no sidecar claims is skipped with a warning."""

    name: str
    x: float
    y: float
    yaw_deg: float = 0.0


@dataclass
class Cue:
    """One line the narrator speaks during a run.

    A brief is one string delivered at t=0, which can only ever express
    "here is the whole task, stated perfectly, up front". Real instructions
    arrive late, get corrected, collide with each other, and share the air
    with conversation that is not addressed to the robot at all. A Cue is the
    smallest thing that lets a challenge express any of that.

    WHEN IT FIRES: once, on the first tick where BOTH conditions hold --
    goal `after_goal` has latched (-1 means "from the start") and the run is
    at least `after_s` seconds old. Gating on goal progress rather than on the
    clock alone is what makes a correction land at a repeatable moment: "as it
    reaches for the red cup" is a place in the task, and a wall-clock time is
    only that place for an agent that happens to move at the reference speed.

    kind:
      "say"     addressed to the robot; acting on it is correct
      "ambient" overheard; acting on it is a FAILURE, and challenges that use
                it pair the cue with a goal asserting the robot stayed put
    """

    text: str
    after_goal: int = -1
    after_s: float = 0.0
    kind: str = "say"
    # For an ambient cue: the prop the line tempts the robot towards. The
    # engine then records how close the robot ever got to it afterwards, which
    # is the only way to tell "carried on with its own job" apart from "went
    # and did the thing it overheard" -- both of which are just movement.
    tempt: str = ""


@dataclass
class Goal:
    """One checklist entry; latches once its predicate reports True.

    Adjacent goals with the same non-empty ``parallel_group`` are all judged
    while that block is active. The following goal remains locked until every
    goal in the block is done.
    """

    label: str
    predicate: Predicate
    parallel_group: str | None = None


@dataclass
class EnvironmentReply:
    """One utterance spoken by a simulated character.

    The challenge owns the words and voice choice; the generic bridge owns
    delivery.  In simulator mode the brain speaks this through Cartesia, shows
    the line in chat as playback starts, and hands it to the agent when it ends.
    """

    speaker: str
    text: str
    voice_id: str


@dataclass
class RuntimeResult:
    """Environment events and chat replies produced by one runtime update."""

    events: list[dict] = field(default_factory=list)
    replies: list[str | EnvironmentReply] = field(default_factory=list)


class ChallengeRuntime:
    """Optional stateful environment behavior attached to one challenge.

    The engine owns lifecycle, trusted-event provenance, and chat transport.
    A runtime owns scenario policy and private state. This keeps concepts such
    as residents, orders, or game rules out of the generic judge.

    ``update`` runs on the physics thread under the engine lock at tick rate:
    return fast, never block, and emit each reply once -- the engine does not
    rate-limit or dedupe.
    """

    def reset(self) -> None:
        """Start a fresh run, discarding all state from the previous one."""

    def update(self, state: WorldState, events: list[dict]) -> RuntimeResult:
        """React to one judged world snapshot and its external events."""
        return RuntimeResult()


@dataclass
class Challenge:
    id: str
    title: str
    brief: str  # shown to the user; what to tell (or do with) the robot
    setup: list[Drop]
    # Adjacent goals with one parallel_group form an unordered phase. Events
    # arriving before their phase are discarded rather than deferred.
    goals: list[Goal]
    # Optional scenario-specific behavior. It is deliberately absent from the
    # public roster/state stream, so private environment facts stay private.
    runtime: ChallengeRuntime | None = field(default=None, kw_only=True, repr=False)
    time_limit_s: float | None = None
    reset_world: bool = True  # robot back to spawn + props re-parked on start
    # Ends the run the moment it holds. See sim/bench/FINDINGS.md (patch_failif)
    # for why a challenge needs a failure that is not the clock.
    fail_if: "Predicate | None" = None
    fail_reason: str = "eliminated"
    # Narrator lines, delivered during the run. Empty for challenges whose
    # whole instruction fits in the brief.
    script: list[Cue] = field(default_factory=list)
    # Which of the three things this scores. 0 means unclassified, which the
    # summary reports separately rather than silently folding into a total --
    # an uncategorised challenge is a challenge nobody decided the purpose of.
    #   1 easy observation and conversation
    #   2 simple instruction following
    #   3 long-horizon instruction following
    # See sim/bench/FINDINGS.md (patch_category) for the rule and its boundary
    # cases.
    category: int = 0


def load_challenges(roots: list[Path]) -> dict[str, Challenge]:
    """Every challenge under `roots`, later roots overriding earlier ones by
    id -- the same shape as props.load_props, so an asset bundle can ship a
    challenge pack next to the props it needs. A broken file is skipped with a
    warning: one bad challenge must not take out the world server.

    Filenames sort the roster the user sees, so they are numbered by what the
    challenge asks for rather than by when it was written: 10s skills, 20s
    people, 30s moving things around."""
    found: dict[str, Challenge] = {}
    for root in roots:
        if not root.is_dir():
            continue
        files = [path for path in root.glob("*.py") if not path.name.startswith("_")]
        packages = [
            path / "__init__.py"
            for path in root.iterdir()
            if path.is_dir() and not path.name.startswith("_") and (path / "__init__.py").is_file()
        ]
        for path in sorted(
            files + packages,
            key=lambda candidate: candidate.parent.name if candidate.name == "__init__.py" else candidate.name,
        ):
            try:
                sidecar_name = path.parent.name if path.name == "__init__.py" else path.stem
                path_key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
                module_name = f"sim_challenge_{sidecar_name}_{path_key}"
                package_paths = [str(path.parent)] if path.name == "__init__.py" else None
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    path,
                    submodule_search_locations=package_paths,
                )
                assert spec and spec.loader
                module = importlib.util.module_from_spec(spec)
                # A path-derived package name supports relative imports without
                # sharing helper-module state across override roots.
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise
                challenge: Challenge = module.CHALLENGE
                found[challenge.id] = challenge
            except Exception as exc:  # noqa: BLE001
                print(f"[challenges] skipping {path.name}: {exc!r}", flush=True)
    return found


# --- engine ---


class ChallengeEngine:
    """Judges one active challenge at a time against the observer state feed.

    Thread model mirrors the world server: start()/abort() run on observer
    connection threads, one at a time under _run_lock, and take the sim lock
    for setup; tick() runs wherever publish_state() does -- the physics thread
    every slice, and the observer thread that just ran a command -- always
    after state gathering, with no sim access of its own (pure evaluation
    under _mutex); post_event() may be called from any thread.

    Lock order is ``_run_lock -> _chat_send_lock -> _mutex``. The sim lock is
    taken under _run_lock during scene setup but released before _mutex; it is
    never held with either chat/engine lock. Anything that changes here has to
    keep those relationships true.
    """

    def __init__(
        self, sim, sim_lock: threading.Lock, roots: list[Path] | None = None, progress_path: Path | None = None
    ):
        self.sim = sim
        self.sim_lock = sim_lock
        self._height_warned = False
        # Tracked source dir plus anything the asset bundle shipped, like the
        # props (core.VirtualMars): a pack can carry its scenarios with it.
        #
        # EXCEPT when the bundle brings its own WORLD. A challenge is only
        # meaningful in the world it was written against -- its Drop
        # coordinates are absolute -- so offering the apartment's scenarios
        # while a 9m authored room is loaded puts props inside walls or off the
        # map entirely, and the goals can never fire. Nothing else enforces
        # that pairing, so a bundle carrying rooms/ owns the roster outright.
        if roots is None:  # an explicitly empty list means "load nothing"
            assets = world.default_assets_dir()
            if (assets / "rooms").is_dir():
                roots = [assets / "challenges"]
            else:
                roots = [world.repo_root() / "sim" / "challenges", assets / "challenges"]
        self.challenges = load_challenges(roots)
        self.progress_path = progress_path or world.repo_root() / "workspace" / "challenges.json"
        self.progress = self._load_progress()
        self._mutex = threading.Lock()  # engine state (active challenge, events)
        # Serializes whole start/abort transitions. start() is three critical
        # sections (deactivate, build the scene, publish the run) and holds
        # nothing across them, so without this two observer connections can
        # interleave and publish one challenge over the other's world. Always
        # taken OUTERMOST -- never while holding _mutex or the sim lock.
        self._run_lock = threading.Lock()
        # Which build of the world a state snapshot came from. Bumped by
        # start() at the top of its scene build and read by publish_state()
        # beside t/pose/centers -- both under the SIM lock, which is what
        # guards it. _run_epoch (ordinary _mutex state) is the epoch the
        # active run was built at: publish_state ticks AFTER releasing the sim
        # lock, so a start() completing in that gap would otherwise have its
        # fresh run judged against the previous one's clock and props.
        self.world_epoch = 0
        self._run_epoch = 0
        # Run tokens keep delayed environment replies out of later scenes.
        self._run_token = 0
        self._chat_inputs: deque[tuple[int, dict]] = deque(maxlen=CHAT_OUTBOX_MAXLEN)
        self._chat_ready = threading.Condition(self._mutex)
        # Orders the final bounded send against run invalidation.
        self._chat_send_lock = threading.Lock()
        self._events: list[dict] = []
        self.active: Challenge | None = None
        self.state = "running"  # of the active challenge: running | passed | failed
        self.reason = ""
        self.goal_done: list[bool] = []
        self.started_t = 0.0
        self._last_judged_t = 0.0  # newest sim time judged; see CLOCK_REWIND_S
        self.elapsed_s = 0.0
        # -- narrator --
        self._cues_fired: set[int] = set()
        self.transcript: list[dict] = []  # every cue spoken, in order, with its sim time
        self._cue_sink = None  # set by the runner that has somewhere to deliver speech
        # -- metrics, accumulated from ground truth the tick already holds --
        # Deliberately only what the ENGINE can see. Turn counts and token
        # spend belong to the agent and are reported by the runner; mixing the
        # two here would let an agent's self-report into the ground truth.
        self.path_len_m = 0.0
        self.goal_times: list[float] = []
        self.utterances = 0
        self.first_utterance_s: float | None = None
        self.tempt_name = ""  # prop an ambient cue pointed at, if any
        self.tempt_min_m: float | None = None  # closest approach to it since
        self._last_xy: tuple[float, float] | None = None
        if self.challenges:
            print(f"[challenges] loaded: {', '.join(self.challenges)}", flush=True)

    # -- persistence --

    def _load_progress(self) -> dict:
        try:
            data = json.loads(self.progress_path.read_text())["challenges"]
        except Exception:  # noqa: BLE001 -- first run or corrupt file: start fresh
            return {}
        # Valid JSON of the wrong shape raises nothing here, only later where
        # the fields are read. Entries of the wrong shape are not screened out
        # field by field: world_server catches the whole challenge layer off
        # the physics thread, which covers every shape rather than the ones
        # thought of here.
        return data if isinstance(data, dict) else {}

    def _save_progress(self) -> None:
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "challenges": self.progress}, indent=2) + "\n")
        tmp.replace(self.progress_path)

    def _record(self, challenge_id: str, result: str, time_s: float | None) -> None:
        entry = self.progress.setdefault(challenge_id, {"attempts": 0, "passed": False, "best_time_s": None})
        entry["last_result"] = result
        entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if result == "passed":
            entry["passed"] = True
            if time_s is not None and (entry["best_time_s"] is None or time_s < entry["best_time_s"]):
                entry["best_time_s"] = round(time_s, 1)
        try:
            self._save_progress()
        except OSError as exc:
            print(f"[challenges] could not write {self.progress_path}: {exc}", flush=True)

    # -- commands (observer connection threads) --

    def start(self, challenge_id: str) -> bool:
        challenge = self.challenges.get(challenge_id)
        if challenge is None:
            print(f"[challenges] start ignored: unknown id {challenge_id!r}", flush=True)
            return False
        # Nothing is judged while the scene is being built. The world reset and
        # the drops take the sim lock, which the physics thread keeps grabbing
        # between ticks, so a tick lands in the middle of this -- and it must
        # not see a challenge whose start time isn't known yet (elapsed_s
        # against a stale started_t instantly "times out" a fresh run) or judge
        # goal 0 against the world the last run left behind. So: deactivate,
        # build the scene, then publish the whole run in one atomic step.
        #
        # _run_lock holds those three steps together against a SECOND starter.
        # Each observer connection commands on its own thread, so two tabs
        # starting different challenges could otherwise interleave -- A drops
        # its props, B's reset re-parks them and drops its own, and whichever
        # publishes last is judged against the other's world, with goals that
        # can never fire. Serialized, the later start simply wins: it aborts
        # the earlier run and builds its scene on top.
        with self._run_lock:
            self._deactivate()
            with self.sim_lock:
                # First, so any snapshot gathered before this build carries the
                # old epoch -- including one already sitting in publish_state's
                # gap, waiting to be ticked.
                self.world_epoch += 1
                if challenge.reset_world:
                    self.sim.reset()  # also re-parks every prop (props.py)
                for drop in challenge.setup:
                    if not self.sim.drop_prop_at(drop.name, drop.x, drop.y, math.radians(drop.yaw_deg)):
                        print(f"[challenges] {challenge.id}: no prop named {drop.name!r} in this world", flush=True)
                started_t = float(self.sim.data.time)
                epoch = self.world_epoch
            with self._mutex:
                for goal in challenge.goals:
                    try:
                        goal.predicate.reset()
                    except Exception:  # noqa: BLE001,S110 -- challenge bug; judged as-is
                        pass
                if challenge.fail_if is not None:
                    try:
                        challenge.fail_if.reset()
                    except Exception:  # noqa: BLE001,S110
                        pass
                self.state = "running"
                self.reason = ""
                self.goal_done = [False] * len(challenge.goals)
                self.started_t = started_t
                self._last_judged_t = started_t
                self._run_epoch = epoch
                self.elapsed_s = 0.0
                self._cues_fired.clear()
                self.transcript = []
                self.path_len_m = 0.0
                self.goal_times = []
                self.utterances = 0
                self.first_utterance_s = None
                self.tempt_name = ""
                self.tempt_min_m = None
                self._last_xy = None
                self._events.clear()  # anything that happened during setup is not this run's
                if challenge.runtime is not None:
                    try:
                        challenge.runtime.reset()
                    except Exception as exc:  # noqa: BLE001 -- challenge bug fails the run, not the sim
                        self.state, self.reason = "failed", f"challenge runtime reset error: {exc!r}"
                entry = self.progress.setdefault(challenge_id, {"attempts": 0, "passed": False, "best_time_s": None})
                entry["attempts"] += 1
                self.active = challenge  # last: judging starts here
                if self.state == "failed":
                    self._record(challenge.id, "failed", None)
        return True

    def abort(self) -> None:
        # Same lock as start(): aborting mid-build would otherwise clear an
        # `active` the starting thread is about to overwrite anyway, leaving
        # its scene on the floor with nothing judging it.
        with self._run_lock:
            self._deactivate()

    def _deactivate(self) -> None:
        """Stop judging. A run still in progress is recorded as aborted --
        whether the user pressed Abort or started something else over it."""
        # Once invalidation owns this lock, no old-token reply can be sent.
        with self._chat_send_lock:
            with self._mutex:
                if self.active is not None and self.state == "running":
                    self._record(self.active.id, "aborted", None)
                self.active = None
                self._run_token += 1
                self._chat_inputs.clear()
                self._chat_ready.notify_all()

    def post_event(self, event: dict) -> None:
        with self._mutex:
            if self.active is not None and self.state == "running":
                self._events.append(event)

    def post_robot_speech(self, text: str, timestamp: float | None = None) -> None:
        """Feed one normal robot chat utterance to the active environment."""
        self.post_event({"type": "robot_speech", "text": text, "timestamp": timestamp or time.time()})

    def _queue_chat_input(self, payload: dict) -> None:
        """Callers hold _mutex. Appending to the full outbox (the publisher
        cannot drain it -- rosbridge down) evicts the oldest reply: say which."""
        if len(self._chat_inputs) == CHAT_OUTBOX_MAXLEN:
            evicted = self._chat_inputs[0][1]
            print(
                f"[challenges] chat outbox full, dropping oldest reply: '{str(evicted.get('text', ''))[:60]}'",
                flush=True,
            )
        self._chat_inputs.append((self._run_token, payload))

    def next_chat_input(self, timeout: float | None = None) -> tuple[int, dict] | None:
        """Wait for the next current-run NPC reply for rosbridge to publish.

        Stale items are discarded here as well as checked immediately before
        publication. Passing zero makes this a non-blocking poll for tests.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._chat_ready:
            while True:
                while self._chat_inputs:
                    token, payload = self._chat_inputs.popleft()
                    if self.active is not None and self.state in ("running", "passed") and token == self._run_token:
                        return token, payload
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._chat_ready.wait(remaining)
                else:
                    self._chat_ready.wait()

    def chat_input_is_current(self, token: int) -> bool:
        """Whether a dequeued reply still belongs to the displayed run."""
        with self._mutex:
            return self.active is not None and self.state in ("running", "passed") and token == self._run_token

    def publish_chat_input_if_current(self, token: int, publish) -> bool:
        """Atomically order one send before or after run invalidation.

        ``publish`` must only perform the already-connected websocket send;
        callers keep reconnect/advertise work outside this lock and enforce a
        finite write deadline.
        """
        with self._chat_send_lock:
            if not self.chat_input_is_current(token):
                return False
            publish()
            return True

    # -- evaluation (physics thread, after state gathering; no sim access) --

    def tick(
        self, t: float, pose: tuple[float, float, float], centers: dict[str, tuple[float, float]], epoch: int
    ) -> dict:
        """Advance the active challenge and return the state-stream block.

        `epoch` is world_epoch, read under the sim lock with t/pose/centers. A
        snapshot from before the active run's scene build carries an older one
        and is rendered but never judged: its t is the previous run's clock (a
        whole server uptime ahead of started_t once the run reset the world --
        an instant "time limit") and its centers are the previous run's props.
        Names the world the numbers came from, so it holds whether or not the
        run rewound the clock."""
        with self._mutex:
            challenge = self.active
            judging = challenge is not None and self.state == "running" and epoch == self._run_epoch
            if judging and t < self._last_judged_t - CLOCK_REWIND_S:
                # Sim time only runs backwards when something rebuilt the world
                # under the run: /virtual_mars/reset, the observer reset op, or
                # core.step()'s NaN recovery -- none of which tell the engine.
                # The run's props are parked, so no goal can pass, and
                # started_t is now ahead of the clock, so elapsed_s clamps to
                # 0.0 and the time limit can never fire either. Left alone that
                # is a 0:00 run nothing but a manual abort can end.
                self.state, self.reason = "failed", "the sim was reset"
                self._record(challenge.id, "failed", None)
                judging = False
            if judging:
                self._last_judged_t = t
                # Drained only when judged: a skipped tick must leave this
                # run's skill completions for the next current one.
                events, self._events = self._events, []
                self.elapsed_s = max(0.0, t - self.started_t)
                # Heights come straight from the sim the engine already holds;
                # object_poses() has carried z all along and nothing read it.
                try:
                    heights = {name: float(p[2]) for name, p in self.sim.object_poses().items()}
                except Exception as exc:  # noqa: BLE001 -- judging must not depend on this
                    # Every min_z goal now fails while heights are missing, so
                    # this must not pass silently: a run that hits it is
                    # producing scores the height rules cannot back.
                    if not self._height_warned:
                        self._height_warned = True
                        print(f"[challenges] heights unavailable ({type(exc).__name__}: {exc})", flush=True)
                    heights = {}
                state = WorldState(t=t, robot=pose, centers=centers, elapsed=self.elapsed_s, heights=heights)
                self._measure(pose, events, centers)
                # Before judging: every goal sees every batch. A goal asserting
                # something about the whole run cannot be judged from the
                # events that happen to arrive on its turn.
                for goal in challenge.goals:
                    try:
                        goal.predicate.observe(state, events)
                    except Exception as exc:  # noqa: BLE001 -- one bad predicate, not the run
                        print(f"[challenges] observe failed on {goal.label!r}: {exc!r}", flush=True)
                if challenge.fail_if is not None:
                    try:
                        # Before the goals: a tick that both eliminates the
                        # robot and would have completed a goal is an
                        # elimination, not a goal followed by one.
                        challenge.fail_if.observe(state, events)
                        if challenge.fail_if.update(state, events):
                            self.state = "failed"
                            self.reason = challenge.fail_reason
                            self._record(challenge.id, "failed", None)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[challenges] fail_if error on {challenge.id}: {exc!r}", flush=True)
                # A guard, not a raised ValueError: upstream removed the
                # raise/catch so a real ValueError from a scenario fails the
                # run instead of reading as "all goals done". An eliminated
                # robot simply judges no goals this tick.
                if self.state == "running":
                    try:
                        if challenge.runtime is not None:
                            runtime_result = challenge.runtime.update(state, events)
                            for event in runtime_result.events:
                                if not isinstance(event, dict):
                                    raise TypeError("challenge runtime events must be dictionaries")
                                events.append({**event, "_source": _ENVIRONMENT_EVENT_SOURCE})
                            for reply in runtime_result.replies:
                                if isinstance(reply, EnvironmentReply):
                                    payload = {
                                        "sender": ENVIRONMENT_SPEECH_SENDER,
                                        "speaker": reply.speaker,
                                        "text": reply.text,
                                        "voice_id": reply.voice_id,
                                    }
                                elif isinstance(reply, str):
                                    payload = {"sender": "user", "text": reply, "timestamp": time.time()}
                                else:
                                    raise TypeError(
                                        "challenge runtime replies must be strings or EnvironmentReply values"
                                    )
                                self._queue_chat_input(payload)
                            if runtime_result.replies:
                                self._chat_ready.notify_all()
                        # next(), not index(): a ValueError escaping this block must
                        # mean a scenario bug and fail the run, not read as "all done".
                        first = next((i for i, done in enumerate(self.goal_done) if not done), None)
                        progressed = False
                        while True:
                            try:
                                idx = self.goal_done.index(False)
                            except ValueError:
                                break
                            goal = challenge.goals[idx]
                            if goal.parallel_group is None:
                                if not goal.predicate.update(state, events):
                                    break
                                self.goal_done[idx] = True
                                progressed = True
                                continue

                            # Every open sibling in the phase sees the same events.
                            group = goal.parallel_group
                            start = idx
                            while start > 0 and challenge.goals[start - 1].parallel_group == group:
                                start -= 1
                            end = idx + 1
                            while end < len(challenge.goals) and challenge.goals[end].parallel_group == group:
                                end += 1
                            for sibling in range(start, end):
                                if not self.goal_done[sibling] and challenge.goals[sibling].predicate.update(
                                    state, events
                                ):
                                    self.goal_done[sibling] = True
                                    progressed = True
                            if not all(self.goal_done[start:end]):
                                break

                        if (
                            first is not None
                            and not progressed
                            and any(ev.get("status") == "completed" for ev in events)
                        ):
                            # Nothing took them, and ordered goals do not defer:
                            # these completions are gone. Say so, or a challenge
                            # author watches a run die on the clock with both tasks
                            # apparently done and no explanation anywhere.
                            skills = sorted({str(ev.get("skill_id") or ev.get("skill_name") or "?") for ev in events})
                            print(
                                f"[challenges] {challenge.id}: dropped completion(s) {', '.join(skills)} -- "
                                f"goals are ordered and {challenge.goals[first].label!r} is still open",
                                flush=True,
                            )
                        while len(self.goal_times) < sum(self.goal_done):
                            self.goal_times.append(round(self.elapsed_s, 2))
                    except Exception as exc:  # noqa: BLE001 -- challenge bug fails the run, not the sim
                        self.state, self.reason = "failed", f"challenge error: {exc!r}"
                        self._record(challenge.id, "failed", None)
                if self.state == "running":
                    if all(self.goal_done):
                        self.state = "passed"
                        self._record(challenge.id, "passed", self.elapsed_s)
                    elif challenge.time_limit_s is not None and self.elapsed_s > challenge.time_limit_s:
                        self.state, self.reason = "failed", "time limit"
                        self._record(challenge.id, "failed", None)
            return self._block(challenge)

    # -- narrator + metrics (called from tick(), already under _mutex) --

    def _measure(self, pose, events: list[dict], state_centers: dict) -> None:
        """Accumulate the cheap ground-truth metrics, then fire due cues.

        Path length is integrated per tick rather than taken as start-to-end
        displacement, because the number worth having is how far the robot
        actually drove: an agent that visits three wrong rooms and comes back
        has the same displacement as one that never moved.
        """
        xy = (pose[0], pose[1])
        if self._last_xy is not None:
            step = math.hypot(xy[0] - self._last_xy[0], xy[1] - self._last_xy[1])
            # A physics hiccup or a reset shows up as a metre-scale jump in one
            # tick; counting it would silently inflate every path length.
            if step < 0.5:
                self.path_len_m += step
        self._last_xy = xy

        for ev in events:
            if ev.get("type") in ("say", "answer"):
                self.utterances += 1
                if self.first_utterance_s is None:
                    self.first_utterance_s = round(self.elapsed_s, 2)

        if self.tempt_name:
            p = state_centers.get(self.tempt_name)
            if p is not None:
                d = math.hypot(xy[0] - p[0], xy[1] - p[1])
                if self.tempt_min_m is None or d < self.tempt_min_m:
                    self.tempt_min_m = round(d, 3)

        self._fire_cues()

    def _fire_cues(self) -> None:
        challenge = self.active
        if challenge is None or not challenge.script:
            return
        done = sum(self.goal_done)
        for i, cue in enumerate(challenge.script):
            if i in self._cues_fired:
                continue
            # after_goal is an INDEX, so -1 means "no goal need be done" and 0
            # means "after the first goal latched".
            if done < cue.after_goal + 1 or self.elapsed_s < cue.after_s:
                continue
            self._cues_fired.add(i)
            line = {"t": round(self.elapsed_s, 2), "kind": cue.kind, "text": cue.text}
            self.transcript.append(line)
            if cue.kind == "ambient" and cue.tempt:
                # Reset rather than accumulate: what matters is the closest
                # approach AFTER the temptation, not before it.
                self.tempt_name = cue.tempt
                self.tempt_min_m = None
            print(f"[narrator] {challenge.id} +{line['t']:.1f}s ({cue.kind}): {cue.text}", flush=True)
            sink = self._cue_sink
            if sink is not None:
                try:
                    sink(line)
                except Exception as exc:  # noqa: BLE001 -- delivery is best effort
                    print(f"[narrator] sink failed: {exc!r}", flush=True)

    def set_cue_sink(self, sink) -> None:
        """Where spoken lines go. The bench runner hands them to its agent; the
        live runner posts them to /brain/chat_in. Without a sink the narrator
        still records a transcript and still fires -- which is what keeps a
        scripted challenge gradeable by an oracle that cannot hear."""
        with self._mutex:
            self._cue_sink = sink

    def metrics(self) -> dict:
        """Everything the engine measured, for the runner's episode record."""
        with self._mutex:
            return {
                "path_len_m": round(self.path_len_m, 2),
                "goal_times_s": list(self.goal_times),
                "utterances": self.utterances,
                "first_utterance_s": self.first_utterance_s,
                "tempt_name": self.tempt_name,
                "tempt_min_m": self.tempt_min_m,
                "transcript": list(self.transcript),
            }

    def roster(self) -> list[dict]:
        """What each challenge IS. Nothing here changes while the server runs,
        so it goes out once per observer connection (world_server.serve_state)
        rather than ~75 times a second -- the briefs are paragraphs."""
        return [{"id": c.id, "title": c.title, "brief": c.brief} for c in self.challenges.values()]

    def _block(self, challenge: Challenge | None) -> dict:
        # Only what can change rides the state stream. Progress is a few
        # numbers per attempted challenge, so it ships every tick rather than
        # in a change-only frame: the stream is latest-wins, and a client that
        # skips the one frame carrying an update would keep a stale roster.
        block = {
            "progress": {
                cid: {
                    "passed": bool(entry.get("passed")),
                    "best_time_s": entry.get("best_time_s"),
                    "attempts": entry.get("attempts", 0),
                }
                for cid, entry in self.progress.items()
                if cid in self.challenges
            },
            "active": None,
        }
        if challenge is not None:
            block["active"] = {
                "id": challenge.id,
                "state": self.state,
                "reason": self.reason,
                "elapsed_s": round(self.elapsed_s, 1),
                "time_limit_s": challenge.time_limit_s,
                "goals": [
                    {"label": g.label, "done": done} for g, done in zip(challenge.goals, self.goal_done, strict=True)
                ],
                # The transcript rides the state stream so any frontend renders
                # the conversation without a second channel to subscribe to.
                "transcript": list(self.transcript),
                "path_len_m": round(self.path_len_m, 2),
            }
        return block


class ChallengeChatBridge:
    """Connect an active challenge runtime to the robot's normal chat topics.

    Robot utterances arrive on ``/brain/chat_out`` and are judged against the
    next ground-truth tick. Environment replies are published to
    ``/brain/chat_in``, where the brain voices a spoken one and decides when its
    line reaches the chat and the agent. Subscribe and publish use separate
    best-effort connections so an idle receive loop cannot strand a reply
    waiting in the engine outbox.
    """

    CHAT_OUT = "/brain/chat_out"
    CHAT_IN = "/brain/chat_in"

    @staticmethod
    def _send_with_timeout(connection, message: str, timeout_s: float = CHAT_WRITE_TIMEOUT_S) -> None:
        """Send once, interrupting a wedged sync websocket at the deadline."""
        if timeout_s <= 0:
            raise ValueError("websocket write timeout must be positive")
        finished = threading.Event()
        timed_out = threading.Event()

        def interrupt_stalled_write() -> None:
            if finished.wait(timeout_s):
                return
            timed_out.set()
            # websockets itself uses shutdown to interrupt a blocking recv on
            # every supported platform; it interrupts sendall as well.
            with suppress(OSError):
                connection.socket.shutdown(socket.SHUT_RDWR)

        watchdog = threading.Thread(target=interrupt_stalled_write, daemon=True)
        watchdog.start()
        try:
            connection.send(message)
        except Exception as exc:
            if timed_out.is_set():
                raise _ChatWriteTimeout("challenge chat websocket write timed out") from exc
            raise
        finally:
            finished.set()
            watchdog.join()

    def __init__(self, engine: ChallengeEngine, url: str = "ws://127.0.0.1:9090"):
        self.engine = engine
        self.url = url
        self._subscribed = threading.Event()
        threading.Thread(target=self._subscribe, daemon=True).start()
        threading.Thread(target=self._publish, daemon=True).start()

    @classmethod
    def robot_speech(cls, message: str) -> tuple[str, float | None] | None:
        """Decode one rosbridge frame, accepting only visible robot speech."""
        frame = json.loads(message)
        if frame.get("topic") != cls.CHAT_OUT:
            return None
        payload = json.loads(frame["msg"]["data"])
        if payload.get("sender") != "robot":
            return None
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        timestamp = payload.get("timestamp")
        return text, float(timestamp) if isinstance(timestamp, int | float) else None

    def _subscribe(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            print("[challenges] `websockets` client unavailable -- environment chat disabled", flush=True)
            return
        announced = False
        while True:
            try:
                with connect(self.url, open_timeout=5) as ws:
                    ws.send(json.dumps({"op": "subscribe", "topic": self.CHAT_OUT, "type": "std_msgs/String"}))
                    self._subscribed.set()
                    if not announced:
                        print(f"[challenges] environment chat listening ({self.url})", flush=True)
                        announced = True
                    for message in ws:
                        try:
                            speech = self.robot_speech(message)
                            if speech is not None:
                                self.engine.post_robot_speech(*speech)
                        except Exception as exc:  # noqa: BLE001 -- junk on the open chat bus
                            print(f"[challenges] ignoring chat event: {exc!r}", flush=True)
            except Exception:  # noqa: BLE001,S110 -- rosbridge down/restarting; retry
                pass
            finally:
                self._subscribed.clear()
            time.sleep(5)

    def _publish(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            return  # the subscriber logged the missing optional dependency
        ws = None
        announced = False

        def open_connection():
            # No advertise: rws resolves the topic type itself.
            connection = connect(self.url, open_timeout=5, close_timeout=CHAT_WRITE_TIMEOUT_S)
            nonlocal announced
            if not announced:
                print(f"[challenges] environment replies connected ({self.url})", flush=True)
                announced = True
            return connection

        # Connect early, but clear of rws startup and its handshake hang
        # (see sim_rosbridge.launch.py): the subscriber proves rws is serving.
        EAGER_CONNECT_GRACE_S = 10.0
        ready_since = None
        while True:
            if ws is None:
                if not self._subscribed.is_set():
                    ready_since = None
                elif ready_since is None:
                    ready_since = time.time()
                elif time.time() - ready_since >= EAGER_CONNECT_GRACE_S:
                    try:
                        ws = open_connection()
                    except Exception:  # noqa: BLE001 -- rosbridge dropped again; re-arm the grace period
                        ws = None
                        ready_since = None
            item = self.engine.next_chat_input(timeout=1.0)
            if item is None:
                continue
            token, payload = item
            while self.engine.chat_input_is_current(token):
                try:
                    if ws is None:
                        ws = open_connection()
                    frame = json.dumps(
                        {
                            "op": "publish",
                            "topic": self.CHAT_IN,
                            "msg": {"data": json.dumps(payload)},
                        }
                    )

                    # Re-check after connection setup, which may have blocked.
                    def send(connection=ws, message=frame):
                        self._send_with_timeout(connection, message)

                    if not self.engine.publish_chat_input_if_current(token, send):
                        break
                    break
                except _ChatWriteTimeout:
                    # Delivery is unknown: the frame may have landed before the
                    # deadline cut the socket, and a resend would repeat the line.
                    print("[challenges] chat write timed out -- reply not resent", flush=True)
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception:  # noqa: BLE001,S110 -- already gone
                            pass
                    ws = None
                    break
                except Exception:  # noqa: BLE001 -- rosbridge down/restarting; retry this current reply
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception:  # noqa: BLE001,S110 -- already gone
                            pass
                    ws = None
                    time.sleep(1)


class SkillEventBridge:
    """Feeds what the robot DOES and what it SAYS into the engine.

    Subscribes over the sim stack's rosbridge websocket (127.0.0.1:9090, JSON
    protocol) to two topics: /brain/skill_status_update for skill lifecycle
    events, and /brain/chat_out so spoken answers can be judged. Without the
    second, every goal about speech is unpassable on the live stack while the
    in-process path judges it fine -- two judges wearing one name. Reconnects
    forever; entirely best-effort -- the sim never depends on it.
    """

    TOPIC = "/brain/skill_status_update"
    CHAT_TOPIC = "/brain/chat_out"

    def __init__(self, engine: ChallengeEngine, url: str = "ws://127.0.0.1:9090"):
        self.engine = engine
        self.url = url
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            print("[challenges] `websockets` client unavailable -- skill events disabled", flush=True)
            return
        announced = False
        while True:
            try:
                with connect(self.url, open_timeout=5) as ws:
                    ws.send(json.dumps({"op": "subscribe", "topic": self.TOPIC, "type": "std_msgs/String"}))
                    ws.send(json.dumps({"op": "subscribe", "topic": self.CHAT_TOPIC, "type": "std_msgs/String"}))
                    if not announced:
                        print(f"[challenges] skill events connected ({self.url})", flush=True)
                        announced = True
                    for message in ws:
                        # Per message: the topic is an open std_msgs/String bus,
                        # so one malformed frame must cost that frame and not
                        # the connection -- a teardown here sleeps 5s, and
                        # rosbridge does not replay what was published meanwhile.
                        try:
                            frame = json.loads(message)
                            if frame.get("topic") == self.TOPIC:
                                self.engine.post_event(json.loads(frame["msg"]["data"]))
                            elif frame.get("topic") == self.CHAT_TOPIC:
                                said = json.loads(frame["msg"]["data"])
                                # Only the ROBOT's own speech. chat_out also
                                # carries system notices ("Brain recovered"),
                                # and letting one of those satisfy a goal would
                                # mean a restart could answer a question.
                                if said.get("sender") not in (None, "system", "user"):
                                    text = str(said.get("text", ""))
                                    if text.strip():
                                        self.engine.post_event({"type": "answer", "value": text})
                        except Exception as exc:  # noqa: BLE001 -- junk on the bus; keep listening
                            print(f"[challenges] ignoring skill event: {exc!r}", flush=True)
            except Exception:  # noqa: BLE001,S110 -- rosbridge down/restarting; retry
                pass
            time.sleep(5)
