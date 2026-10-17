# Six-world environment refresh — 15 September 2026

Rounds, Workshop, Pantry, Counter, Bridge and Blaze now open as furnished,
populated worlds. Their camera-visible finishes are authored in
`mars_sim_driver/scene_details.py`, with structural colliders in each room
sidecar. The top camera re-frames when a world changes or the robot respawns. Decorative overlays are non-colliding and separated in depth.

| World | Visible changes | Task corrections |
|---|---|---|
| Rounds | Framed art, runner, furnished rooms, red hardback book, blue delivery mat | Correct 45/60/80/110 cm doorway widths; explicit pauses and door-count scope; book delivery to the visible floor mat |
| Workshop | Tool boards, steel-and-oak benches, labeled paint and oil cans, yellow handled mug | All nine task props in free play; briefs name the objects and required approach/pause |
| Pantry | Jar and box bay signs, sorting mats, textured labeled stock | Jars on left shelves, boxes on others; floor staging for transfers; Stocktake includes the final return instruction |
| Counter | Café prints, fluted counter, recognizable cups and teapots, delivery mats | Required pickups on the floor; deliveries to visible mats with height bounds and settling time |
| Bridge | Teal gate frames, brass thresholds, symmetric guide strips, ordinal markers | Three-gate brief matches the five-gate room; disfluent route stays available in the original prompt |
| Blaze | Kitchen details, study screen, bed, framed art, exit signs, identifiable belongings | Required pickups moved off shelves/desks to clear floor; save order, exit and hazard deadlines stated in the brief |

## Scene and scoring contract

`Prop.initial_pose` populates free play. Starting a challenge resets with
`populate=False` and places exactly its setup, so extra display stock cannot
change a counting answer. A challenge can explicitly use `populate_world=True`
when its tasks depend on a shared exhibit (Gallery does). Respawn restores the
free-play layout; Retry restores the selected challenge.

Missing setup props now refuse to start and report a scene setup error rather
than starting an impossible episode and scoring it against the agent. Floor
delivery predicates require known height within the object's floor interval
and 0.75 seconds in the destination. This rejects airborne or buried objects;
it does not independently verify gripper detachment.

The current live pickup skill projects targets onto the floor. Fifteen tasks
in Counter, Pantry and Blaze previously required unsupported shelf pickup;
they now use floor pickup and floor release areas. Counter's deliberately
unreachable teapot still tests recognition of a capability limit. Blaze's
level-four photo remains an intentional item to leave behind. Timed hazard
rules drive visible, animated flames and rising smoke in both the viewer and
robot RGB cameras. The fire starts at the stove (and the level-four bed),
spreads through the judge’s timed regions, and resets on Retry. These effects
do not collide or appear in the depth map; combustion and smoke transport are
not physically simulated.

In free play, fire grows from the stove across the kitchen, then reaches the
east hall, study and bedroom over five minutes. This is the same schedule as
Evacuation 1's 5:00 countdown: kitchen unsafe at 4:00 elapsed, east hall at
4:30, study at 4:45, and the final bedroom spread at 5:00. The medicine task
allows four minutes for search and pickup, then a full minute to escape.
The west hall, store room and porch remain free of fire. Respawn restarts
free play; Retry resets the fire and countdown together. The other evacuation
challenges retain their own timelines. Flames spread outward from
each ignition point and new patches grow smoothly from zero.

A timing regression drives the medicine route with 105 seconds of initial
delay and another 90 seconds at pickup. It still finishes with over 30 seconds
remaining, and its return route stays clear even at maximum fire spread.
This uses oracle-assisted placement and does not validate live grasping.

These changes revise the task layouts. Historical benchmark scores, including
the old shelf-versus-floor controls, are not directly comparable. The three
Counter skill-event controls still require real arm completion events and are
reported NEEDS-ARM by the scripted gate. Teleport-assisted oracle placement
checks navigation and judge logic; it does not demonstrate live grasp success.

## Assets and validation

From the repository root:

```sh
sim/.venv/bin/python sim/tools/build_benchmark_props.py
sim/.venv/bin/python sim/tools/build_pantry_props.py
export PYTHONPATH=$PWD/ros2_ws/src/mars_bot/mars_sim_driver:$PWD/sim/bench
MUJOCO_GL=glfw sim/.venv/bin/python -m pytest sim/bench/test_environment_refresh.py -q
MUJOCO_GL=glfw sim/.venv/bin/python sim/bench/main.py \
  --map rounds --map workshop --map pantry --map counter --map bridge --map blaze \
  --agents oracle,random --seeds 2 --workers 3
```

Use EGL or OSMesa instead of GLFW on headless Linux. OBJ/PNG assets feed the
robot camera; matching GLBs feed the viewer. The generators author the artwork
and packaging locally. Nav maps are exported with movable objects parked so
free-play props do not become permanent navigation obstacles. Re-export and
stage the maps after structural changes, then reload Nav2's active map.

The refresh regression checks cover all 38 exact setups, free-play population,
floor support and reachable approaches, shared OBJ/GLB geometry and textures,
missing-prop rejection, bounded delivery heights, and simultaneous floor
placements on the Blaze porch. All six worlds were also switched through the
live observer API, checking their prop counts and served GLB assets.

## Validation result

| World | Scripted oracle passes | Random completions (two full-length seeds) |
|---|---:|---:|
| Rounds | 5/5 | 0/10 |
| Workshop | 4/4 | 0/8 |
| Pantry | 4/4 | 0/8 |
| Counter | 15/15 | 1/36 |
| Bridge | 3/3 | 0/6 |
| Blaze | 4/4 | 0/8 |

Gate totals: **35 VALID**, **3 NEEDS-ARM**, **0 INVALID**, **0 INCOMPLETE**.

The initial Workshop fetch oracle put the can on its own chassis; after
correcting floor placement, the targeted oracle rerun passed. The combined
result preserves the original failed episode as provenance. No hosted brain
or real-arm evaluation was run. Regression validation: 86 Python tests, five
viewer environment tests, TypeScript type-check and production viewer build.
The results are stored in `sim/bench/results/six_world_refresh_validated.json`.
