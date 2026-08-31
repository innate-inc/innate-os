"""The new agent: NemotronStackBackend.

See AGENT_SPEC.md for the architecture and what it targets. This file is the
implementation-reality half of that document: what could actually be built
and run against this harness, with every substitution for unavailable
hardware (real NemotronLabs VoiceChat, a physical Jetson) named at the call
site rather than glossed over.

THE PROTOCOL. One harness turn == at most two CONVERSATIONAL model calls,
not an open tool-loop. The grounding tool makes its own vision call on top
of those, and retries once on a low-confidence read, so a turn that grounds
costs three model calls and four in the retry case. Only check_reach and
explore_frontier are free -- they are pure Python:

  call 1 (the "conversational core" call) -- sees the frame, the task-stack,
  the last tool result if any, and may reply either with a real action, or
  with a request to consult exactly one tool first.

  if it asked for a tool: the backend runs it (ground_object is a second,
  separate Gemini vision call with its own narrower prompt; check_reach and
  explore_frontier are pure Python, no network) and makes call 2 with the
  result appended -- which MUST return a real action, no further tool
  requests honoured. This bounds every turn to two CONVERSATIONAL calls and
  mirrors the SHAPE of NemotronLabs' "tool call without stopping the
  conversation" -- the conversation call is re-issued once the tool result
  exists rather than held open. In this harness it is still synchronous:
  decide() blocks on every call. The non-blocking property belongs to the
  target architecture, not to this stand-in.

NO OVERFITTING. Nothing here references a challenge id, a prop name, or a
map. `grep -i "counter_\\|blaze_\\|household_\\|pantry_\\|gallery_\\|workshop_\\|rounds_\\|bridge_" backends_v2.py`
returns exactly ONE line: this docstring paragraph, which necessarily
contains the pattern text itself to state it. Any OTHER line matching is
the thing to fix.
"""

from __future__ import annotations

import base64
import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from backends import _coerce, _last_json_object  # reused, not duplicated
from reach_tool import can_reach, standoff_for

# The robot's real, stated physical properties (camera mounting height, field
# of view) -- not simulator ground truth, not benchmark content. Any MARS
# unit has these; a deployment against different hardware would change these
# two numbers and nothing else in this file.
CAMERA_HEIGHT_M = 0.25
FOV_DEG = 90.0

GROUND_CACHE_TTL_S = 4.0
POSE_BUCKET_M = 0.30
POSE_BUCKET_DEG = 15.0

CONVERSATION_SYSTEM = """You are the reasoning core of a small mobile robot (camera
25 cm up, ~90 degree field of view) doing what a person asks. You act one
step at a time and see the result of each step before choosing the next.

You keep a TASK-STACK -- goals still owed, incidental facts worth
remembering, things you must not falsely claim -- across the WHOLE episode,
not just the last few turns. It is shown to you every turn. Update it
whenever something changes: a goal finishes, a new one is added mid-task, a
correction supersedes an earlier one, someone tells you something you might
be asked to recall later. Emit updates as an optional "task_stack" object
alongside your action:

  {"goals": [{"id": "<short id you choose>", ...}], "facts": {"<key>":
   "<value>"}, "constraints": ["..."], "done": ["<id>"]}

Updates MERGE, they do not replace -- you never need to re-list everything
you are already tracking, only what changed. A goal is added if its "id"
is new, or updated in place if you reuse an existing "id". To FINISH a
goal, name its "id" in "done" -- leaving it out of "goals" does NOT remove
it, it stays active (the far more common case is you just forgot to
re-type it). "facts" and "constraints" only ever grow: say something once
and it stays remembered without repeating it every turn.

Some "facts" starting with "released:" are written automatically by the
robot itself the instant your gripper lets go of something, not by you --
trust these as ground truth for "I am no longer holding it". They do NOT
mean the item ended up in the right place, only that you released it and
where; you still have to judge, from what you saw, whether that was
correct. If one is present, you already let go of that item -- do not
claim you never picked it up or never had it, and do not re-open whether
you CAN reach or pick it up as if that were still an unsolved problem you
have to re-litigate -- you already acted on it. A fresh height or
distance estimate from the current frame can be noisy from read to read
for the same physical object; it is not by itself a reason to contradict
something you already did. If asked about that item again for the SAME
reason, answer from what you did and where you left it, not a new guess.
If you are asked to handle it AGAIN -- fetch it back, move it elsewhere
-- that is a new task: go to where you left it and check reach from
where you stand then, like any other object; a stale "released:" fact
does not excuse skipping that.

You have two tools, used by replying with {"tool": "<name>", "args": {...}}
INSTEAD OF an action, when you need their answer before you can decide:

  ground_object  args: {"description": "the red cup"} -- ask what the
      camera can tell you about a named thing's bearing, distance and
      height, instead of eyeballing pixels yourself.
  check_reach    args: {"bearing_deg": ..., "distance_m": ..., "height_m": ...}
      -- ask whether something at that estimate is within the arm's reach
      RIGHT NOW, or where you would need to stand for it to be. Use this
      BEFORE attempting a pick, not after "nothing within reach" tells you.
  explore_frontier  args: {} -- when nothing relevant is in frame and you
      need to search, ask for the least-recently-looked-at direction rather
      than guessing where to turn.

You will be asked again immediately after a tool answers, and must give a
real action that turn -- tools do not chain.

Be efficient: every action costs a turn. Drive in useful distances, not 5 cm
nudges. If something is genuinely beyond the arm (check_reach said so),
report that plainly instead of trying anyway or refusing before checking.
Not everything you hear is addressed to you -- people talk to each other in
the same room; act only on what you were actually asked."""

GROUND_SYSTEM = """You are the robot's vision module. You are given one camera
frame and the name of a thing to find in it. The camera sits {h} m above the
floor, level (not tilted), with a {fov} degree horizontal field of view and
square pixels -- so its focal length in pixels is
f = (frame_width_px / 2) / tan({fov}/2 in radians), and a row r (0=top) sees
straight ahead at row = frame_height_px/2 (the horizon). Reply with EXACTLY
one JSON object, nothing else:

  {{"found": true|false, "bearing_deg": <float, 0=straight ahead, +left>,
   "distance_m": <float or null>, "height_m": <float or null>,
   "confidence": "low"|"medium"|"high"}}

USE THIS GEOMETRY, don't eyeball it:
1. DISTANCE. Find where the thing (or whatever it visibly rests on -- a
   shelf, a table, its own base) touches the floor, or find a support
   directly below it that itself touches the floor (a shelf unit's foot, a
   table leg). Read that contact point's row r_floor. Then
   distance_m = (f * {h}) / (r_floor - horizon_row), using the frame's own
   height in pixels for horizon_row and f as defined above. This is exact
   pinhole geometry, not an approximation -- trust it over a size guess.
2. HEIGHT. Once you have distance_m (from step 1, or from tracking the
   object across it and the floor-contact point sharing a support), read the
   OBJECT's own row r_obj (its base, where it rests). Its height above the
   floor is height_m = {h} - (r_obj - horizon_row) * distance_m / f. An
   object sitting on the floor gives height_m near 0, which is a sanity
   check the formula gives you for free.
3. If you cannot find any floor-contact point to anchor step 1 (nothing
   visibly grounds the object or its support to the floor), you cannot
   compute a real number -- report confidence "low" and your best rough
   estimate, do not silently fall back to a guess dressed up as a formula
   result.

If you cannot see the object at all, "found": false and leave the rest
null."""


GOAL_CAP = 40  # pathological-growth backstop only, see apply()'s docstring
CONSTRAINT_CAP = 20


def _goal_id(g: dict) -> str | None:
    """A usable, stable string key for a goal dict's "id", or None if it
    has none. Deliberately excludes bool (a subtype of int in Python --
    `isinstance(True, int)` is true -- which would otherwise silently
    accept it) and empty string: both are valid-looking but would let two
    UNRELATED goals collapse onto the same key ("" from one dict, "" from
    another; True/False from a model that answers a yes/no-shaped
    question instead of choosing a real id) with no error and no trace.
    Anything else scalar is coerced with str() -- this is deliberately
    lenient (numeric ids are a real, observed model behavior, not a
    hypothetical), matching how "done" is coerced the same way."""
    gid = g.get("id")
    if gid is None or isinstance(gid, (list, dict, bool)):
        return None
    text = str(gid)
    return text if text else None


class _TaskStack:
    __slots__ = ("goals", "facts", "constraints", "scanned_bearings", "start_yaw", "dropped")

    def __init__(self) -> None:
        self.goals: list[dict] = []
        self.facts: dict[str, str] = {}
        self.constraints: list[str] = []
        self.scanned_bearings: list[float] = []
        self.start_yaw: float | None = None
        # Counts task_stack entries that could not be applied (missing/
        # unusable "id"). Not wired into the episode report -- this pass
        # adds visibility inside the object, not a harness-wide metrics
        # change -- but it means a sweep CAN be checked for silent id-
        # convention non-adoption instead of that only being discoverable
        # by reading transcripts by hand.
        self.dropped: int = 0

    def as_text(self) -> str:
        return json.dumps(
            {"goals": self.goals, "facts": self.facts, "constraints": self.constraints},
            separators=(",", ":"),
        )

    def note_released(self, item: str, pose: tuple[float, float, float] | None, elapsed_s: float | None = None) -> None:
        """Mechanically checkpoint a verified gripper release into facts --
        called from the harness-observed carrying transition (see
        decide()), never from the model's own say-so. Same "verify
        mechanically, do not trust self-report" principle as reach_tool.py's
        geometry, applied to memory instead of geometry.

        Named "released", not "placed" or "delivered": this fires on ANY
        transition from carrying something to carrying nothing, including
        a place at the wrong destination or a put-down mid-route. It is
        ground truth for "no longer holding it", not for "task complete" --
        the model still has to judge whether where it happened was right.
        The position AND time are recorded because the harness already
        knows both for free at the moment this fires (the robot does not
        move between a place action and the next observation) and
        re-deriving "where and when did I leave that" from scratch is
        exactly the class of avoidable work this exists to remove."""
        x, y = (round(pose[0], 2), round(pose[1], 2)) if pose else (None, None)
        where = f",at({x},{y})" if x is not None else ""
        when = f",t={elapsed_s:.0f}s" if elapsed_s is not None else ""
        self.facts[f"released:{item}"] = f"true{where}{when}"

    def apply(self, update: object) -> None:
        """Merge a model-reported update INTO the stack -- never replace it
        wholesale. The previous version did `self.goals = [...]` and
        `self.constraints = [...]` on every call (up to twice a turn): a
        full replace, not a merge. A single reply that re-lists its goals
        incompletely -- which a flash-tier model over a 30+ turn episode
        will do -- silently deleted whatever it forgot to re-type, with no
        way back. `facts` already used dict.update() (a real merge) and
        never showed this failure mode; goals/constraints are fixed to
        match that discipline, not loosened to match the old bug.

        goals: merged by the model-supplied "id" (see CONVERSATION_SYSTEM).
        An id already present is updated in place; a new id is appended; an
        id is removed ONLY if the update names it in "done" -- never by
        silent omission. An id is coerced with str() (accepting 1 as well
        as "1": a flash-tier model emitting a bare number here is a real,
        not hypothetical, failure mode) -- ONLY a goal with no id at all
        (None, missing, or a non-scalar like a list/dict) is dropped as
        malformed, and dropping it increments `dropped` rather than
        silently vanishing with no trace. This matters: an earlier version
        of this fix required id to already be a str, which meant a model
        that never adopts the id convention gets EVERY goal discarded
        forever -- strictly worse than the destructive-replace bug being
        fixed, since replace at least kept the latest snapshot. Coercing
        is what makes "merge, don't replace" actually safer than the
        original bug in the case where the model does not cooperate, not
        only in the case where it does. `done` is coerced the same way and
        also accepted as a single bare id, not only a list -- a model that
        finishes exactly one goal and sends `"done": "gate1"` instead of
        `"done": ["gate1"]` must not have that silently ignored.

        goals get a generous, rarely-hit cap (GOAL_CAP) as a backstop
        against unbounded growth from id drift (a typo'd id creates a
        permanent duplicate instead of updating the original -- exact-
        match merging cannot fix a typo, and is not trying to; catching
        near-duplicates would need fuzzy matching, which risks silently
        merging two goals that only happen to look similar, a worse
        failure than a rare duplicate). This is disclosed as a real,
        unsolved gap, not hidden behind the cap.

        constraints: append-only, de-duplicated, capped at CONSTRAINT_CAP,
        with RE-MENTION REFRESHING POSITION (delete-then-append) so a
        constraint the model keeps correctly re-stating is not the one
        that ages out. These are the prompt's own words for "things you
        must not falsely claim" -- exactly the class of information a
        memory bug must never lose. An earlier version of this fix kept
        first-occurrence order under dict.fromkeys(), which meant an
        important early constraint, even if faithfully re-asserted every
        turn, still fell off the cap once 20 unrelated later ones arrived
        -- reintroducing a milder copy of the exact bug this method exists
        to fix. Refreshing on re-mention is the difference between a cap
        that forgets what is old and one that forgets what is unused.
        """
        if not isinstance(update, dict):
            return
        done = update.get("done")
        if isinstance(done, (str, int, float)) and not isinstance(done, bool):
            done = [done]
        self._merge_goals(update.get("goals"), done)
        self._merge_facts(update)
        self._merge_constraints(update)

    def _merge_goals(self, goals_update: object, done: object) -> None:
        """Upsert reported goals by id and remove the ones named done.

        "done" is its own top-level key, not nested under "goals" -- a
        reply that ONLY finishes a goal ({"done": ["gate1"]}, no "goals"
        list at all) must still remove it. An earlier version of this
        method nested the done-handling inside `if goals_update: ...`,
        which silently no-op'd on exactly that reply shape -- caught by
        test_taskstack.py before this ever reached a review, not after.
        """
        if isinstance(goals_update, list) or isinstance(done, list):
            by_id: dict[str, dict] = {}
            for g in self.goals:
                gid = _goal_id(g)
                if gid is not None:
                    by_id[gid] = g
            if isinstance(goals_update, list):
                for g in goals_update:
                    if not isinstance(g, dict):
                        self.dropped += 1
                        continue
                    gid = _goal_id(g)
                    if gid is None:
                        self.dropped += 1
                        continue
                    # Pop-then-reinsert, not a plain overwrite: Python dicts
                    # keep an EXISTING key's original position on update, so
                    # a plain `by_id[gid] = g` on an id that is already
                    # present would NOT move it to the end -- meaning the
                    # cap below evicts by INSERTION order, not by recency,
                    # and preferentially kills off the one goal that keeps
                    # getting legitimately updated every turn while a pile
                    # of stale, never-touched, typo'd-id duplicates (the
                    # exact failure this cap exists to bound) survive
                    # because they are individually newer insertions. Found
                    # by running the cap's own motivating scenario, not by
                    # inspection.
                    by_id.pop(gid, None)
                    by_id[gid] = g
            if isinstance(done, list):
                for gid in done:
                    gid = _goal_id({"id": gid})
                    if gid is not None:
                        by_id.pop(gid, None)
            capped = list(by_id.values())[-GOAL_CAP:]
            if len(by_id) > GOAL_CAP:
                self.dropped += len(by_id) - GOAL_CAP
            self.goals = capped

    def _merge_facts(self, update: dict) -> None:
        """Facts are a plain overlay: last writer wins, values stringified."""
        if isinstance(update.get("facts"), dict):
            self.facts.update({k: str(v) for k, v in update["facts"].items()})

    def _merge_constraints(self, update: dict) -> None:
        """Append-only and de-duplicated, with a re-mention refreshing
        position so the cap forgets what is unused rather than what is old."""
        if isinstance(update.get("constraints"), list):
            incoming = [str(c) for c in update["constraints"]]
            merged = [c for c in self.constraints if c not in incoming] + incoming
            self.constraints = list(dict.fromkeys(merged))[-CONSTRAINT_CAP:]


def _wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


class NemotronStackBackend:
    """See module docstring and AGENT_SPEC.md. Substitutes Gemini for the
    real NemotronLabs VoiceChat call (no NIM/Nemotron API access in this
    environment) -- everything downstream of that one call (task-stack
    discipline, the two real tools, the bounded two-call protocol) is real,
    not mocked."""

    wants_image = True
    wants_pose = True
    # This backend's real cost is however long two sequential Gemini HTTPS
    # calls happen to take -- an artifact of standing in for the real
    # architecture, not something the real architecture would pay. The
    # target system's tool calls do not stop the conversation (NemotronLabs
    # VoiceChat's headline feature) and its measured turn-taking latency is
    # ~450 ms; charging THAT instead of wall-clock is the same reasoning
    # runner.py's think-charge mechanism already applies to claude_bridge.py,
    # for the identical reason -- see runner.py's think-charge comment.
    think_charge_s = 1.2  # ~1 core call + occasional async tool call, at the target latency

    # Same model tier innate-os's own brain runs (.env: "GEMINI_MODEL
    # overrides the brain's model (default gemini-3.6-flash)"). Deliberate:
    # the comparison this backend exists to produce is architecture vs
    # architecture, not model vs model -- swapping to a stronger model here
    # would confound the one variable this experiment is testing.
    def __init__(self, model: str = "gemini-3.6-flash", timeout_s: float = 90.0) -> None:
        self.key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not self.key:
            raise RuntimeError("GEMINI_API_KEY is not set; refusing to run a vision agent blind")
        self.model = model
        self.timeout_s = timeout_s
        self.base = os.environ.get("GEMINI_BASE_URL", "").strip() or "https://generativelanguage.googleapis.com"
        self.reset()

    def reset(self) -> None:
        self.stack = _TaskStack()
        self._ground_cache: dict[tuple, tuple[float, dict]] = {}
        self._last_tool_result: str | None = None
        self._last_carrying: str | None = None

    # -- the one external call shape, reused for both the core and vision calls
    def _call(self, system: str, obs, extra_text: str, want_image: bool) -> dict:
        parts = [{"text": extra_text}]
        if want_image and obs.image_path:
            blob = base64.b64encode(Path(obs.image_path).read_bytes()).decode()
            parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": blob}})
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"},
        }
        req_data = json.dumps(body).encode()
        # A transient 5xx over a 45-episode sweep is a network fact, not an
        # agent decision -- two retries keeps that noise out of the results
        # instead of recording "backend error" for a turn the model never
        # actually got to reason about. The loop exits by break (success) or
        # raise (non-retryable code, or the last attempt) -- nothing falls
        # through it.
        for attempt in range(3):
            req = urllib.request.Request(
                f"{self.base}/v1beta/models/{self.model}:generateContent?key={self.key}",
                data=req_data,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = _last_json_object(text)
        return parsed if parsed is not None else (json.loads(text) if text.strip() else {})

    # -- tool: cloud VLM grounding call, separate from the core call on purpose
    def _ground_object(self, obs, description: str) -> dict:
        pose = obs.robot_pose or (0.0, 0.0, 0.0)
        bucket = (
            description.strip().lower(),
            round(pose[0] / POSE_BUCKET_M),
            round(pose[1] / POSE_BUCKET_M),
            round(math.degrees(pose[2]) / POSE_BUCKET_DEG),
        )
        cached = self._ground_cache.get(bucket)
        if cached and time.time() - cached[0] < GROUND_CACHE_TTL_S:
            return cached[1]
        prompt = f'Find this object in the frame: "{description}"'
        system = GROUND_SYSTEM.format(h=CAMERA_HEIGHT_M, fov=FOV_DEG)
        result = {"found": False}
        # One retry on a low-confidence read, not more: this is hedging
        # against a noisy single call (real sensor practice -- a real depth
        # estimate would not need this), not tuning toward any answer.
        for _attempt in range(2):
            try:
                result = self._call(system, obs, prompt, want_image=True)
            except Exception as exc:  # noqa: BLE001 -- a grounding failure must not crash the turn
                result = {"found": False, "error": f"{type(exc).__name__}: {exc}"}
                break
            if result.get("confidence") != "low":
                break
        self._ground_cache[bucket] = (time.time(), result)
        return result

    # -- tool: reachability, pure geometry, no network
    def _check_reach(self, obs, bearing_deg: float, distance_m: float, height_m: float | None) -> dict:
        pose = obs.robot_pose
        if pose is None or distance_m is None:
            return {"error": "no pose or distance estimate available"}
        x, y, yaw = pose
        world_bearing = yaw + math.radians(bearing_deg)
        tx = x + distance_m * math.cos(world_bearing)
        ty = y + distance_m * math.sin(world_bearing)
        tz = height_m if height_m is not None else 0.05
        verdict = can_reach((x, y), (tx, ty, tz))
        out = {
            "reachable": verdict.reachable,
            "horizontal_m": verdict.horizontal_m,
            "height_m": verdict.height_m,
            "reason": verdict.reason,
        }
        if not verdict.reachable:
            so = standoff_for((tx, ty), tz)
            if so is None:
                out["standoff"] = None
                out["standoff_note"] = "no standing position helps -- height alone rules it out"
            else:
                # Express the standoff as a heading+distance FROM HERE, since
                # that is what the core call can act on directly.
                dx, dy = so.x - x, so.y - y
                out["standoff"] = {
                    "bearing_deg": round(math.degrees(math.atan2(dy, dx) - yaw), 1),
                    "distance_m": round(math.hypot(dx, dy), 2),
                }
        return out

    # -- tool: frontier-style scan memory, pure bookkeeping, no network
    def _explore_frontier(self, obs) -> dict:
        pose = obs.robot_pose
        yaw_deg = math.degrees(pose[2]) if pose else 0.0
        if self.stack.start_yaw is None:
            self.stack.start_yaw = yaw_deg
        rel = _wrap_deg(yaw_deg - self.stack.start_yaw)
        # Snap to the nearest un-scanned 60-degree slice around the full
        # circle -- a systematic pan-and-remember, not a lookup table.
        SLICE = 60.0
        candidates = [round(_wrap_deg(k * SLICE)) for k in range(int(360 / SLICE))]
        unscanned = [
            c for c in candidates if not any(abs(_wrap_deg(c - s)) < SLICE / 2 for s in self.stack.scanned_bearings)
        ]
        target = (
            unscanned[0]
            if unscanned
            else min(
                candidates,
                key=lambda c: (
                    min(abs(_wrap_deg(c - s)) for s in self.stack.scanned_bearings)
                    if self.stack.scanned_bearings
                    else 0
                ),
            )
        )
        self.stack.scanned_bearings.append(rel if unscanned else target)
        turn_needed = _wrap_deg(target - rel)
        return {
            "turn_degrees": round(turn_needed, 1),
            "note": f"{len(unscanned) - 1} of {len(candidates)} directions still unscanned after this",
        }

    def decide(self, obs, menu) -> dict:
        # Mechanical checkpoint, not model-reported: obs.carrying is the
        # harness's own verified gripper state (brain_agent.py's _pick/
        # _place), not something this backend can get wrong by forgetting
        # to ask for it. A transition from carrying something to carrying
        # nothing can only mean _place just ran (the only way _carrying
        # clears), so it is recorded as a fact regardless of whether the
        # model itself said anything about it this turn.
        prev_carrying, self._last_carrying = self._last_carrying, obs.carrying
        if prev_carrying is not None and obs.carrying is None:
            self.stack.note_released(prev_carrying, obs.robot_pose, obs.elapsed_s)
        elif prev_carrying is None and obs.carrying is not None:
            # The item just picked back up may be one this stack already
            # marked "released:" from an earlier pick/place -- that fact is
            # now stale (the gripper is holding it again, not "no longer
            # holding it") and re-released below will write a fresh one when
            # it next lets go. Left alone, a strengthened "don't re-litigate
            # a released item" prompt instruction would tell the model to
            # trust a fact that now contradicts obs.carrying itself.
            self.stack.facts.pop(f"released:{obs.carrying}", None)
        extra = (
            f"TASK-STACK: {self.stack.as_text()}\n"
            + (f"TOOL RESULT (from your last request): {self._last_tool_result}\n" if self._last_tool_result else "")
            + f"{obs.as_text()}\n\n{menu}\n\n"
            'Reply with an action as usual, OR with {"tool": name, "args": {...}} '
            "if you need one of the two tools first. You may also include "
            '"task_stack": {...} to update goals/facts/constraints.'
        )
        self._last_tool_result = None
        reply = self._call(CONVERSATION_SYSTEM, obs, extra, want_image=True)
        self.stack.apply(reply.get("task_stack"))

        if "tool" in reply and "action" not in reply:
            name = str(reply.get("tool", "")).strip()
            args = reply.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            if name == "ground_object":
                result = self._ground_object(obs, str(args.get("description", "")))
            elif name == "check_reach":
                result = self._check_reach(
                    obs,
                    float(args.get("bearing_deg", 0.0)),
                    float(args.get("distance_m", 0.0)),
                    args.get("height_m"),
                )
            elif name == "explore_frontier":
                result = self._explore_frontier(obs)
            else:
                result = {"error": f"unknown tool {name!r}"}
            self._last_tool_result = json.dumps(result, separators=(",", ":"))
            extra2 = (
                f"TASK-STACK: {self.stack.as_text()}\n"
                f"TOOL RESULT ({name}): {self._last_tool_result}\n"
                f"{obs.as_text()}\n\n{menu}\n\n"
                "Now give a real action -- no more tool requests this turn."
            )
            reply = self._call(CONVERSATION_SYSTEM, obs, extra2, want_image=True)
            self.stack.apply(reply.get("task_stack"))

        return _coerce(reply)
