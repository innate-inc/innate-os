# Spatial-memory benchmark

Everything here runs the **production** spatial-memory code from
`brain_client` (store, admission policy, Gemini retrieval with context
caching) against the MuJoCo apartment — no reimplementations. Run all
commands from `sim/`.

## 1. Capture a dataset

```bash
uv run benchmarks/spatial_memory/capture.py
```

Drops 9 recognizable props around the apartment (banana and can in the
kitchen, red mug by the sofa, airpods beside the bed, blue book and cube in
the office, green backpack in the living corner, sock in the corridor, ball
in the dining nook), then walks the main camera through every room —
teleport-glide at 0.35 m/s, 1 frame per sim-second, with look-around sweeps
at each stop. ~160 frames in ~3 s wall. Output under `dataset/home_tour/`:

| file | what |
|---|---|
| `frames/NNNN.jpg` + `manifest.jsonl` | every frame with stamp, x, y, yaw, room |
| `scene.json` | settled prop poses, room registry, tour + capture meta |
| `data/` | a **production-format** memory: `maps/sim_apartment.pgm` + `spatial_memory/sim_apartment/{index.json, N.jpg}`, built by feeding every frame through `plan_admission` (39 of 161 admitted — exactly what the robot's recorder would keep). Point `INNATE_OS_ROOT` at it and the robot loads it as-is. |

`--mode drive` uses real physics (cmd_vel + wedge-recovery) instead of
gliding; `--probe "x,y,yaw_deg"` renders one frame to `probe.jpg` for
viewpoint tuning. Tour waypoints, room bounds, and prop placements live in
`apartment.py` — every stop and leg is validated against free cells of the
collision occupancy grid (see the docstrings there for the door-threading
gotchas).

## 2. Annotate ground truth

```bash
uv run benchmarks/spatial_memory/annotate.py
```

Raycasts from the camera to every prop in every frame → `visibility.json`
(which frames actually show which props, with distance and bearing). Every
placed prop is visible in ≥1 admitted memory; airpods and backpack in exactly
one — the deliberately hard targets.

## 3. Run the retrieval benchmark

```bash
uv sync --group benchmark
uv run benchmarks/spatial_memory/run_benchmark.py --fake oracle   # harness check, no API
GEMINI_API_KEY=... uv run benchmarks/spatial_memory/run_benchmark.py \
    --models gemini-3.6-flash,gemini-3.6-pro
```

`tasks.json` holds 33 tasks in 5 categories: **find_object** ("Find my
airpods"), **find_location** ("Go to the kitchen"), **instruction** ("Exit
the room" — with robot context), **open_ended** ("I am hungry"), and
**honesty** ("Find the cat" — the right answer is no-match). Scoring: 1.0
when the returned frame shows a gold prop (per `visibility.json`) or lands in
a gold room/region, 0.5 for right-room-wrong-frame on object tasks, 1.0 for
an honest miss on honesty tasks. Baselines: oracle = 1.00, always-frame-1 =
0.12.

Compare axes:

- `--models a,b,c` — models run in **parallel threads**, each with its own
  context cache; tasks run sequentially within a model to reuse it.
- `--cache` (default) warms the production Gemini context cache first — the
  robot's steady state, measuring cached latency. `--no-cache` sends all
  frames inline per call (cold-boot behavior).
- `--query-style context|raw` — whether task context (e.g. "robot is in the
  master bedroom") is prepended, emulating how the agent should enrich
  queries.

Reports land in `results/<stamp>-<mode>/{results.json, report.md}`: overall +
per-category accuracy, mean latency, cached fraction, per-task reasons.

## 4. End-to-end challenges (full agent stack)

Six sidecars in `sim/challenges/` run the whole loop — chat → agent →
search_memory → navigation — judged by the challenge engine in the sim
web app (`https://localhost` → challenges):

| id | category | test |
|---|---|---|
| `memory_kitchen` | find location | "Go to the kitchen" |
| `memory_airpods` | find object | "Find my airpods" (6 cm target, distractors) |
| `memory_hungry` | open-ended | "I am hungry" → banana in the kitchen |
| `memory_exit_room` | instruction | enter bedroom, then just "Exit the room" |
| `memory_rounds` | long horizon | dining → office → bedroom, in order |
| `memory_thirsty` | open-ended | either the can or the mug counts |

Give the robot a memory of the apartment first — tour it by teleop, or set
`INNATE_OS_ROOT` to a captured `dataset/home_tour/data` — then the memory
challenges measure recall-driven navigation instead of blind exploration.

## Webapp panel (sim only)

`innate-sim up` also starts a host-side benchmark service ([service.py](service.py),
port 8801, `innate-sim logs benchmark`), and the sim webapp grows a
**Benchmark** section: capture datasets, browse what the admission policy
kept (thumbnails with per-frame prop visibility), launch retrieval runs with
a live log, and compare results — all from `https://localhost`. The rail
entry, the route, and the `/benchmark` proxy path exist only when the webapp
runs beside a sim (`WEBAPP_SIM_CONTROLS`); a robot's webapp has none of it.
Live Gemini runs need `GEMINI_API_KEY` in the repo `.env` (the launcher
injects it into the service).

## Scaling / speed notes

- `VirtualMars` is unpaced: the full capture tour is ~3 s wall; N instances
  can run in one process for parallel eval rollouts (rendering is lazy,
  ~8 ms per 640×480 frame — 1 fps equivalents are effectively free).
- Retrieval benchmark cost is dominated by the Gemini calls; the warm cache
  makes repeated searches cheap, which is exactly the production behavior
  under test.
- The camera rides ~0.26 m off the floor: tabletops are invisible, so
  benchmark objects live on the floor. Prop `drop_z` defaults to floor
  height — set it explicitly (see `32_mug.py`) to land props on furniture.
- The admission policy (1 m / 100°) means a look-around heading within 100°
  of the arrival heading is skipped as redundant — plan look angles (or
  object placements) accordingly, or they never enter the store.
