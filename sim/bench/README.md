# Benchmark

A task suite for the innate-os agent, plus the harness that runs it and the
gate that decides whether a task is allowed to report a number at all.

**Evidence.** The raw material behind `FINDINGS.md` -- full probe
transcripts, repeat/trace records and per-episode JSONs -- lives on the fork
branch [`benchmark-evidence`](https://github.com/Hcoder10/innate-os/tree/benchmark-evidence/sim/bench/results),
kept out of this PR to keep it mergeable -- including the per-episode
score files (`nemotron_stack_results.json`, the live baseline's
`eval/*.log`) that the numbers in `NEMOTRON_STACK_RESULTS.md` are read
from. A sweep writes fresh ones under `results/` (gitignored).

## Running it

Headless, from anywhere in a clone, on `sim/.venv` (the system python has no
mujoco):

```bash
export MUJOCO_GL=osmesa
export PYTHONPATH=$PWD/ros2_ws/src/mars_bot/mars_sim_driver

sim/.venv/bin/python sim/bench/main.py --list      # what backends and challenges exist
sim/.venv/bin/python sim/bench/main.py             # the whole suite: 45 challenges, oracle + random
```

That second command is the benchmark. Everything else is a narrowing of it:

```bash
main.py --map rounds                          # one world
main.py --challenges within_reach             # one challenge, by id or substring
main.py --agents oracle                       # solvability only
main.py --agents oracle,random,brain:nemotron_stack   # score an agent (needs GEMINI_API_KEY)
debug_one.py household household_tour         # trace a single episode
python -m pytest sim/bench -q                 # offline tests: no key, no network
```

Results stream to `sim/bench/results/bench_results.json` as they land.

### Pointing it at your own agent

An architecture is a class with `decide(observation, menu) -> action`. Name it
on the command line -- a built-in key, or an import path to anything on the
`PYTHONPATH`:

```bash
main.py --agents brain:nemotron_stack           # built-in (see --list)
main.py --agents brain:your_module:YourBackend  # your own, no harness edit
```

`registry.py` holds the built-in names. Adding an architecture does not
require touching the harness -- that is the point of the seam, and
`--agents brain:<module>:<Class>` is the whole interface.

### The live benchmark

innate's own stack -- Docker, ROS2, nav2, the brain -- scored by the same
judge, one command from anywhere in a clone:

```bash
bash sim/bench/run_eval.sh              # all 8 maps; backend picked from .env
bash sim/bench/run_eval.sh counter      # one map
```

It needs either `INNATE_SERVICE_KEY` (their proxy, preferred) or
`GEMINI_BASE_URL` (a local shim, `gemini_shim.py`) in `.env`. It restarts the
stack once per map, and writes per-category episode JSONs plus a stamped log
under `sim/bench/results/eval/`.

No ROS. `VirtualMars` and `ChallengeEngine` directly, one episode per process,
`imap_unordered` so results stream to `sim/bench/results/bench_results.json`
as they land (a sweep that is killed part-way still leaves data -- and they
live beside the harness, not in /tmp, because systemd-tmpfiles cleaned /tmp
mid-sweep twice and took the results with it).

One episode per process is deliberate: `core.py` reads `ASSETS_DIR` at import
time, so a worker that ran a second episode for a different map would silently
keep building the first map's world.

## The validity gate

A challenge counts only if:

| | |
|---|---|
| the **oracle passes** | it is solvable, so a failure is the agent's |
| **random fails** | it is not solvable by flailing, so a pass means something |

ARC-AGI-3 screened 414 candidate environments down to 135 on the second rule
alone. A suite that has not been screened reports numbers that look like
capability and are not.

This gate has earned its place. It started at 4/13 on the hand-written maps,
and **every one of those failures was a defect in the challenge or the oracle,
not an agent result**:

- a goal that required a height change — `add_planar_base` gives the robot x, y
  and yaw only, so it can never be on top of anything, and no agent however
  good could pass it
- a 0.35 m doorway, when the base is 0.188 m wide: 8 cm of total clearance
- arrival slop quietly eating goal radii, so an approach 0.45 m from a target
  left the robot 0.63 m away against a 0.5 m radius
- a carry model that re-placed the carried object 0.6 m ahead every tick,
  making it an obstacle the robot drove into forever
- an approach that drove to a prop's exact centre, shoved it, and then chased
  the target it had just displaced
- a "person" that is really a 1.7 m body lying on the floor (see
  `sim/props/20_human.py`), which landed across the only approach

None of these announce themselves. Each shows up as an agent scoring zero.

### Verdicts

- `VALID` — oracle passed, random failed.
- `NEEDS-ARM` — the goals need `SkillDone`, so a scripted base agent cannot
  witness solvability. Held to the weaker half of the rule (random must still
  fail) and reported separately rather than folded into the pass count.
- `INVALID` — random passed (measures nothing), or the oracle could not solve
  it. `no path to (x, y)` distinguishes an unreachable goal from a timeout.
- `INCOMPLETE` — not every agent ran.

## Oracles are derived, not written

`autoplan.py` builds the reference plan from the challenge's own goals.
`Near("robot", "sock", 0.5)` means stand next to the sock; `InRect("robot", …)`
means be in this box; `InCircle("mug", x, y, r)` means put the mug there. Goals
are strictly ordered and latching, which is exactly what makes a plan derivable
at all.

`planner_agent.py` executes those steps by A*-ing over the sim's own occupancy
grid (`navplan.py`). This matters more than it sounds: with a straight-line
follower, "the oracle failed" conflated *this challenge is broken* with *my
follower cannot get out of a room*, and there is no reading of a benchmark
where those mean the same thing.

Carrying is abstract on purpose — `grab` remembers a prop and `put` teleports
it. These plans certify that goals are reachable and geometry is navigable, and
claim nothing about manipulation. Challenges that genuinely need the arm are
classified `NEEDS-ARM` rather than pretended to be solved.

## What is measured

Eight purpose-built maps, 45 challenges across the three categories the brief
names (13 observation/conversation, 17 simple instruction, 15 long-horizon):

| map | isolates |
|---|---|
| **counter** | conversation: counting, clarification, mid-task correction, overheard speech, implicit requests, a remembered detail — plus fetch/deliver, with floor-vs-shelf twin controls |
| **pantry** | counting under classification pressure (a misfiled item counts as what it IS), shelving, a five-goal stocktake |
| **workshop** | reach (5 benches, 0.06–0.30 m tops), grasp band (5 cans, 40–100 mm), occlusion |
| **gallery** | height above the floor plane (5 identical mugs, 0–0.5 m) and bearing (8 identical cans at 45°) |
| **rounds** | doorway clearance (0.35–1.00 m), room identification by fixture, long-horizon delivery |
| **household** | composite: four rooms, three stations, no isolated variable |
| **bridge** | spoken route-following, clean list vs the same route delivered disfluently (the pair prices disfluency in gates) |
| **blaze** | urgency: a spreading fire (hard fail), reprioritisation cues, evacuation ordering |

Scene furniture on the Gallery sits at 22.5° off-bearings, exactly between ring
positions, so every can has the same plain-wall backdrop. Furniture parked on a
ring bearing would make some cans easier to detect and quietly turn a
search-coverage probe into a contrast probe.

## Known limits, stated rather than buried

- **Random is capped** (`--random-cap`, default 240 sim-seconds) while oracles
  run to the challenge's own limit. "Random failed" means *did not succeed
  within that budget*, not *proven impossible*. The run prints the cap.
- **Three real agents have run.** innate's own brain did the full live suite on
  Aug 16 (4/45, $7.60, ~4.5 h — see `results/eval/` and FINDINGS.md; a subset
  of tasks changed after that run and is re-scored separately, marked in the
  files). Separately, a strong general model was wired in as the brain over a
  file bridge (`claude_bridge.py`) and played every challenge from the camera
  and transcript alone — a third validity layer beyond oracle/random: where IT
  passes, the task is certified doable by something bound by the robot's own
  rules, and its per-episode fairness verdicts are what found most of the
  defects FINDINGS.md records. Third, this project's own submitted agent
  (`backends_v2.py`, `brain:nemotron_stack`) ran the full 45-challenge suite
  in-process — scores, methodology caveats and before/after re-runs in
  `NEMOTRON_STACK_RESULTS.md`.
- **Oracle step counts are not difficulty.** `household_tour` takes 15,602
  steps against `rounds_all_doors`'s 1,862; that is a statement about the
  reference plan, not the map.

## Sim constraints worth knowing before writing a challenge

- The robot **cannot change height**. `add_planar_base` gives x, y, yaw only.
- The base is **0.188 × 0.182 m**, so it physically fits every door in the
  suite, down to the 0.35 m one the oracle drives through. What stops the robot
  at the narrow end is planner inflation, not geometry.
- `pose()` returns qpos directly — yaw is **radians**.
- A floor must sit at **exactly z = 0**. Two millimetres proud is a penetration
  the planar base can never rise out of, and it pins the robot: commanded to
  spin at 1 rad/s it managed 2° in 1.5 s.
- `Geom.condim` must be **3**, not 4. The friction triple is (sliding,
  torsional, rolling), and condim 4 switches the torsional term on.
- A prop's `drop_z` is where it is RELEASED. `Drop` carries no z, so a prop
  meant to sit on a plinth must have a `drop_z` above it or it lands on the
  floor.
