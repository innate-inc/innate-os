# A new MARS agent: spec

Designed against what this benchmark actually found broken, not against a
wishlist. Every decision below cites the finding that motivated it. Where a
decision is NOT backed by a finding, it is marked speculative and left out
of the implementation.

## What the current system loses to, and why

From 137 live episodes (Aug 16 + Aug 17 runs) and 92 probe episodes:

| Failure class | Live score | Probe score (same robot, same rules, stronger reasoning) | Diagnosis |
|---|---|---|---|
| See-and-say, no locomotion | 3/3 of the pure-perception tasks | same | not broken -- keep |
| Simple fetch/deliver | ~1/2 goals typical, rarely completes | 36/45 challenges certified solvable | approach + timing, not perception |
| Room-name search (no map) | 0/9 (household), 0/5 (rounds search tasks) | mostly solvable from camera alone | **no exploration policy**, and no bridge from "bathroom" to a place to look |
| Long-horizon (3+ step) | 0/15 both live runs | 33/33 goals when the probe held the thread | **no persistent task state** -- the chat history IS the plan, and it leaks |
| Mid-task correction | flaky (route_change: pass/fail/pass across 3 runs) | 100% once cue delivery was fixed | correction retrieval, not correction *hearing* |
| Manipulation approach | 0 successful shelf/counter grasps, ever, live | reliable once armed with real distance math | **no reachability check** -- the robot free-drives into "nothing within reach" |
| Honesty about limits | `out_of_reach` 0/3 both runs | 3/3 once given the arm's real height ceiling as a fact | the model was never TOLD its own ceiling as structured data |

Five distinct root causes, not one. A bigger LLM fixes zero of them by
itself -- they are missing capabilities, not missing intelligence. That is
the design brief for everything below.

## Architecture

The cloud tier is not one model doing two jobs -- it is two models running
CONCURRENTLY, on the interaction-model / background-model split Thinking
Machines Lab published this year: a fast model stays continuously present
in the conversation (turn-taking, backchannel, deciding when something is
worth interrupting for), while a separate, heavier model does the sustained
reasoning -- planning, tool use, holding the task-stack -- fed "a rich
context package, not a standalone query" and streamed back in, interleaved
"at a moment appropriate to what the user is currently doing" rather than
as an abrupt cut-in. That is a materially better fit for this robot's worst
failure classes than a single model alternating between talking and
thinking: long-horizon tasks (0/15 in both live runs) are a sustained-
reasoning problem, which is the BACKGROUND model's job by design, while
conversation retention and mid-task corrections need a model that never
stops being present, which is the INTERACTION model's job by design. One
model trying to be both is what produced this session's own coherence bug
(see the results doc) -- it has no structural way to separate "what am I
saying right now" from "what is my actual plan."

```
  CLOUD / WORKSTATION GPU (two models, concurrent, sharing context)
  ┌─────────────────────────────┐        ┌─────────────────────────────┐
  │  INTERACTION MODEL           │        │  BACKGROUND MODEL             │
  │  NemotronLabs VoiceChat      │◄──────►│  a stronger general LLM       │
  │  (11B, full-duplex, ~450ms   │  full  │  -- sustained reasoning,      │
  │   turn-taking, streams in    │ convo  │     task-stack ownership,     │
  │   200ms micro-turns)         │context │     exploration planning,     │
  │  -- stays present, decides   │        │     tool use (reach check,    │
  │     WHEN a background result │        │     VLM grounding, skills)    │
  │     is worth surfacing       │        │  -- works without blocking    │
  └───────────────┬───────────────┘        │     the live conversation     │
                  │                        └───────────────┬───────────────┘
                  │ skill goals                              │ streamed results,
                  ▼                                          │ interleaved not dumped
      ┌────────────────────┐                                 │
      │  Skill dispatch     │◄────────────────────────────────┘
      │  (existing          │
      │   innate-os skills) │
      └──────────┬──────────┘
                 │
      ┌───────────────────────────────────────────────────────────┐
      │           Jetson Orin Nano 8 GB -- ON-ROBOT                │
      │  ─────────────────────────────────────────────────────    │
      │  • VAD + turn-taking gate (what audio leaves the robot)    │
      │  • reachability/standoff tool (geometry, no model)         │
      │  • frontier-exploration policy (geometry + costmap)        │
      │  • rolling utterance buffer (survives a cloud hiccup)      │
      │  • local backchannel ("mm-hm") while the interaction model │
      │    is mid-turn, same masking role either architecture needs│
      │  NOT hosted here: EITHER cloud model, or the VLM.          │
      │  8 GB shared RAM tops out around 7B before it is the       │
      │  bottleneck -- an 11B+ audio-native model does not fit     │
      │  alongside everything else this device has to do.          │
      └───────────────────────────────────────────────────────────┘
```

### Why the split is drawn here, specifically

**The duplex model is cloud, full stop.** NemotronLabs VoiceChat ships as an
NVIDIA NIM container -- that is a datacenter/workstation deployment target,
not an edge one. Independently: the Jetson Orin Nano 8 GB has 8 GB of
*shared* CPU+GPU LPDDR5 at 68 GB/s, and real-world testing tops out around
7B parameters before that memory becomes the bottleneck. An 11-12B model
that also has to hold audio encoder/decoder state does not fit next to
everything else the robot needs running (nav, costmap, camera pipeline, this
architecture's own local tier). Claiming otherwise would be exactly the kind
of unreproducible number this benchmark's whole discipline exists to catch.

**The VLM is a tool the background model calls, not a third cloud tier of
its own** -- grounding is occasional, not every control tick, and the
background model already owns tool dispatch, so routing it through a
separate service adds a hop without adding anything. It stays cloud for
the same reason the background model does: there is no latency argument for
pulling either on-device the way there is for the interaction model's
turn-taking.

**What actually earns its place on the Jetson** is the stuff that is either
too latency-critical to round-trip to the cloud, or needs no model at all:

- **Reachability tool** -- pure geometry (`reach_tool.py` in this repo,
  described below), microseconds, and it is queried before every
  manipulation attempt. Round-tripping this to the cloud would add latency
  for zero benefit; it needs no model, so it does not compete for the 8 GB.
- **Frontier exploration** -- turn-toward-the-most-unexplored-bearing is a
  costmap query against data nav2 already has locally. No model needed.
- **VAD / turn-taking gate** -- must run at audio-frame latency; this is
  exactly what small on-device VAD models are for, and it is the one place
  a tiny (sub-100M) local model belongs.
- **Backchannel mask** -- while the cloud duplex model composes a real
  reply, the local tier can emit a filler ("mm-hm", a head-nod skill call)
  so the robot is not silent for the round-trip. This is a real, shipped
  technique in production voice systems, not novel to this spec -- it is
  listed because the take-home explicitly asks "where does the latency
  actually go," and the honest answer for a cloud-duplex-model design is
  "a network round-trip on every turn, masked locally."
- **Rolling utterance buffer** -- the last ~30 s of transcribed speech,
  kept locally, replayed to the cloud model on reconnect. Directly answers
  H10/H16 (the deaf-live-robot fault) and `route_change`'s flakiness: a
  correction spoken during a connectivity blip must not be lost.

## The three mechanisms that fix the five root causes

Not five separate fixes -- three general mechanisms, each targeting more
than one failure class, none of them benchmark-specific.

### 1. Reachability tool (`sim/bench/reach_tool.py`, built and tested this session)

Two calls: `can_reach(robot_pose, target)` -- reachable from here, yes/no
and why; `standoff_for(target)` -- where would I need to stand, or `None` if
no standing position helps (the honest out-of-reach case). Both are closed-
form geometry using the SAME constants the harness judges pick/place
against, imported not re-typed, so the tool can never disagree with the
judge about what counts as reachable.

This directly targets the manipulation-approach failure (no more blind
"drive an estimate, get told no, re-estimate" loops -- probe transcripts
show this costing 6-10 turns per fetch) and the honesty failure
(`out_of_reach`'s pass condition is exactly "know you can't reach it and say
so" -- this tool makes that a lookup instead of a guess).

General on purpose: it takes a position and a robot pose, nothing about
"cups" or "teapots" or challenge IDs. It would answer the same question in
a kitchen it has never seen.

### 2. Task-stack: persistent structured plan state

The single largest failure class (long-horizon, 0/15 in both live runs) is
not a reasoning failure -- probe agents solved every one of these tasks once
they could keep track of what they'd already done. The current system's
only memory is the chat transcript, which is exactly the wrong data
structure for "what am I still owed": it grows, it isn't queryable, and a
correction three turns back is buried under six turns of skill-status noise.

The fix is a small persistent object, not a bigger context window:

```json
{
  "goals": [
    {"desc": "red cup -> left seat", "status": "done"},
    {"desc": "blue cup -> right seat", "status": "pending", "added_by": "customer, mid-task"}
  ],
  "facts": {"wifi_password": "teapot42"},
  "constraints": ["never claimed red cup out of reach"]
}
```

Read and written via an explicit tool call (`update_task_stack`), checkpointed
on every skill completion. It is compact -- tens of tokens, not hundreds --
so it costs almost nothing to keep in every context window, which is exactly
the "what gets cached" question the brief asks about: the task-stack is the
one piece of state that is NEVER dropped from context, while raw skill-status
chatter is summarized-and-discarded after each goal closes.

This is also what fixes `route_change`-style flakiness and the `carried_detail`
memory probe: an incidental fact ("wifi password is teapot42") goes in
`facts`, not into a transcript position that decays with distance.

General on purpose: `goals`/`facts`/`constraints` are generic slots. Nothing
about counters or stools. The same object handles a five-room tour or a
three-item evacuation.

### 3. Frontier exploration: a policy, not a bigger model

The other half of the long-horizon and search failures is that "find the
bathroom" has no bridge from a room *name* to a place to *look*, and the
current system's response to that gap is undirected wandering. NemotronLabs
VoiceChat being a better conversationalist does not fix this -- it is a
missing skill, not a missing IQ point.

The fix: an `explore_frontier()` tool that keeps scan memory -- which
60-degree slices of the circle the robot has already looked at -- and turns
toward the nearest one it has not, reporting back "now facing an unexplored
area, camera attached." It is bookkeeping over the robot's own heading, NOT
a costmap query: nav2 builds an occupancy grid, and wiring this tool to it
is the obvious next step, but the shipped version does not read it. The duplex model's job becomes: look at
what the VLM grounds in that frame, decide if it matches the target
("sink and toilet visible" -> stop; "bookcase" -> call `explore_frontier()`
again). This is precisely what probe transcripts show top-performing agents
doing by hand (systematic 45-60° pans, bearing tracking to avoid re-visiting)
-- codified as a callable skill instead of relying on the model to invent
the strategy fresh every episode.

Explicitly NOT a semantic map, NOT room-name lookup, NOT uninavid (excluded
per this project's earlier instruction). It is a general "where haven't I
looked" primitive that composes with the VLM's "does this match" judgment --
the same two-part structure a human uses to find something in an unfamiliar
building.

## Context, caching, latency -- the unglamorous part

The two-model split changes what "context" even means here: it is not one
window, it is a shared conversation handed whole to a second process, plus
each model's own private working state.

**What's in the INTERACTION model's context, every turn:**
- System prompt (fixed, small)
- Streaming conversation state (audio-native micro-turns, so this is the
  model's own rolling state, not a text log this design manages)
- The single most recent thing the background model surfaced, IF this is a
  moment appropriate to say it -- not queued and dumped, held until a
  natural gap. Everything else the background model is doing stays out of
  this model's context entirely; it does not need the task-stack, only the
  next sentence worth saying.

**What's in the BACKGROUND model's context, every turn:**
- The full conversation (handed over as a package when work is delegated,
  per Thinking Machines' own description of the pattern -- "not a
  standalone query")
- Task-stack (compact JSON, always present, rewritten in place not
  appended)
- The MOST RECENT tool result only (VLM grounding, skill status, reach
  verdict) -- older tool results are folded into the task-stack if they
  matter and dropped if they don't. This is the single biggest departure
  from the current system, whose context apparently grows with every skill
  event (see H12/H18 in FINDINGS.md: think-time billing blew up specifically
  because nothing was pruning tool-call history).

**What's cached:**
- The VLM grounding result for a named object, keyed on (object description,
  robot pose bucketed to 0.3 m / 15°), TTL 4 s. Prevents re-paying a cloud
  VLM round-trip for "where's the cup" on every single tick while
  approaching it -- only re-grounds after real movement or on staleness.
- The reachability tool's answer is NOT cached -- it's microseconds, caching
  it would be optimizing something that isn't the bottleneck.

**Where the latency actually goes:**
- Interaction-model turn-taking: ~450 ms (NemotronLabs' own measured figure
  on Full-Duplex-Bench 1.0) -- this never waits on the background model; it
  is the floor of the live conversation regardless of what else is running.
- Background-model reasoning + tool calls: unbounded relative to the live
  conversation by design -- this is the entire point of the split. A
  multi-step plan can take several seconds to work out without the person
  ever hearing dead air, because the interaction model is not blocked on
  it. This is the direct fix for H1/H18 (sim time charged 1:1 against
  wall-clock model latency, meaning every call in the CURRENT system WAS
  blocking) -- not a workaround for it, an architecture that doesn't have
  the failure mode.
- VLM grounding: budgeted 300-800 ms, one of the background model's own
  tool calls -- invisible to the interaction model entirely.
- Reachability/exploration tools: <5 ms, local, never the bottleneck.
- Network round-trip, interaction model: the one number this spec can't
  give without the real deployment link; budgeted 50-150 ms each way,
  masked by local backchannel when it's the model composing a full reply
  rather than an instant ack.
- Network round-trip, background model: not latency-sensitive the same
  way -- it is allowed to be slow, that is what makes the split work.

## No-overfitting rules, followed while designing this

- No challenge ID, prop name, or map name appears in any tool's logic --
  verified: `grep -i "counter_\|blaze_\|household_"` across the new files
  matches only the line of `backends_v2.py`'s own docstring that states
  this same rule (the pattern text necessarily matches itself); any other
  match is the thing to fix.
- The task-stack schema is generic (goals/facts/constraints); it was not
  shaped around any specific challenge's goal count or ordering.
- The reachability tool's constants are imported from the harness's own
  judged constants, not re-derived by staring at which challenges pass.
- Frontier exploration is a costmap algorithm, not a lookup table of "the
  bathroom is at (x, y)."
- The implementation (next section) was built and then run against the
  full 45-challenge suite in one pass, with no iteration on prompts or
  parameters in response to which challenges failed. Later fix rounds
  (FINDINGS.md T17/T18/T20) DID change task-stack code and prompt text in
  response to observed failures; their effects are reported as separate
  before/after re-verification tables in NEMOTRON_STACK_RESULTS.md,
  never folded back into the headline sweep numbers.

## Implementation reality, stated plainly

This session has no NVIDIA NIM / Nemotron API access, no access to Thinking
Machines' interaction-model API (research preview, partner access only as
of its announcement), and no physical Jetson Orin Nano. What's real and
what's substituted, explicit:

| Piece | Status |
|---|---|
| Reachability tool | **Real.** Pure geometry, tested against the harness's actual judged constants (see above), no substitution needed. |
| Frontier exploration | **Real, but simpler than the name suggests.** Scan memory over 60-degree heading slices, driven by the robot's own yaw. It does not read the occupancy grid or a costmap; "frontier" describes the intent, not the data structure. |
| Task-stack | **Real.** A JSON scratchpad read/written via tool call, checkpointed on skill completion. |
| Two-model concurrent split | **Prototyped and measured, then removed from this PR.** A background thread ran the heavy model continuously while the interaction thread took whatever was already finished. Measuring it surfaced a real harness bug -- `max_turns` charged the harness's own "nothing ready yet" filler tick as if the robot had thought, so every episode died of turn exhaustion rather than on the task. That fix lives in `brain_agent.py` and is still in (FINDINGS.md T16). The backend itself was an unfinished experiment whose own docstring recorded an unported checkpoint, so it is not worth the lines in a submission. |
| Duplex conversational core | **Substituted.** Implemented against Gemini (the API this session has), prompted to honor the same contract (tool calls that don't block conversation, task-stack discipline). NemotronLabs' actual audio-native turn-taking and interrupt handling are NOT reproduced -- this session's harness is text/image-turn-based, not audio-streaming, so "full-duplex" is honored as a *context and tool-calling discipline*, not literally demonstrated. Swapping in the real model means replacing this one backend class; nothing else in the harness changes, which is itself the demonstration of the take-home's second requirement. |
| Cloud VLM | **Substituted with the same Gemini key**, but called as a SEPARATE, explicitly labeled tool path rather than folded into the main decision call -- so the architecture's separation of concerns is real in the code's structure even though both slots happen to hit the same API today. |
| Local VAD / backchannel | **Not implemented.** No audio pipeline exists in this harness to attach it to. Documented here as the honest gap rather than faked. |

Backend: `sim/bench/backends_v2.py`, class `NemotronStackBackend`, named in
`registry.py` and runnable as `--agents brain:nemotron_stack` through the
unmodified harness. Any other architecture needs no registry entry at all:
`--agents brain:<module>:<Class>` resolves an import path directly.
