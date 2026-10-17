# Findings

Which failures were the benchmark's fault, and which were the robot's.

This is the report. The full chronological ledger -- every entry with its
evidence, the wrong theories, the fixes that did not work -- is
[`FINDINGS_FULL.md` on the evidence branch](https://github.com/Hcoder10/innate-os/blob/benchmark-evidence/sim/bench/FINDINGS_FULL.md),
beside the transcripts and per-episode JSON it cites.

## How a failure gets attributed

A challenge reports a number only if it passes a validity gate: the scripted
oracle must solve it (the world permits the outcome) and the random agent must
fail it (it cannot be passed by flailing). A third layer, a strong agent given
the robot's own tools -- the skills innate's sim gives its default agent on
main, read from main's sources -- and far more time, passed all 45 (73
episodes, 10 Oct 2026). An earlier probe on eight harness actions had
certified 36.

So when the robot fails a VALID challenge, the world allowed it, chance could
not do it, and a stronger agent could. That is what makes "the robot's fault"
a measurement rather than an opinion.

**HARNESS** -- the apparatus was wrong, and the number it produced was not
about the robot. **TASK** -- the challenge itself was wrong, caught before it
reported anything. **ROBOT** -- the system under test. **LIVE STACK** --
real deployment faults in the Docker/ROS2 path.

## Scores

| | current system (live, audited) | new agent (in-process) |
|---|---|---|
| challenges | 5/45 | 6/45 |
| goals | 22/121 | 33/121 |
| cat 1 -- observation & conversation | 3/13 | 3/13 |
| cat 2 -- simple instruction | 2/17 | 3/17 |
| cat 3 -- long-horizon | 0/15 | 0/15 |

The gap is the robot's, not the benchmark's: 45 of 45 have been passed by an
agent using the robot's own tools, 5 by the robot. Long-horizon is a wall rather than a slope -- 0/15 for both,
in two independent live runs.

## Every fault found, and whose it was

56 entries. One line each; the full account of any of them is in
`FINDINGS_FULL.md` under the same id.

| id | class | finding |
|---|---|---|
| `H1` | HARNESS | Sim time ran ten times faster than the wall clock while the agent thought |
| `H2` | HARNESS | A well-formed model reply was reported as an agent failure |
| `H3` | HARNESS | A blind agent was offered an action that needed eyes |
| `H4` | HARNESS | Results were written to `/tmp`, which the OS emptied mid-sweep |
| `H5` | HARNESS | `Said(negate=True)` could never be true |
| `H6` | HARNESS | The ambient-compliance metric flagged the one agent that provably could not comply |
| `H8` | HARNESS | A stalled leg spent the whole clock, then blamed the clock |
| `H7` | HARNESS | The porter ignored `CanCollide` |
| `H16` | HARNESS | Every live run was played to a deaf robot |
| `H17` | HARNESS | The probe driver served every map the first map's world |
| `H18` | HARNESS | The probe was billed for the plumbing's think time |
| `H19` | HARNESS | The in-process pick was invisible to the skill judge |
| `H20` | HARNESS | A carried object stayed physically where it was picked |
| `T1` | TASK | A 0.60 m counter put its own centre line out of reach |
| `T2` | TASK | The room was five times too big for the robot in it |
| `T3` | TASK | "Say you can't reach it" is passed perfectly by an agent that always says that |
| `T4` | TASK | A landmark that does not survive the camera is not a landmark |
| `H15` | TASK | The lint reported every map but the first as clean |
| `H13` | TASK | A one-character quoting bug invalidated every episode at once |
| `H14` | TASK | A hung model call would have deadlocked the episode forever |
| `H10` | TASK | The vision agent had the camera's name wrong, inside a try/except |
| `H11` | TASK | Rendering at a sixteenth of the real camera |
| `H12` | TASK | The agent had no way to give an answer the engine could hear |
| `H9` | TASK | The nav map ignored anything under 10 cm — and this robot cannot climb 1 mm |
| `T7` | TASK | An approach goal scored a behaviour the brief never asked for |
| `T6` | TASK | A gate that passes only for the reference plan is not a gate |
| `T8` | TASK | Conversation was measured through the gripper |
| `T9` | TASK | Every seat circle floated 0.42 m in front of its stool |
| `T10` | TASK | Rubrics that demanded more than their briefs said |
| `T11` | TASK | Objects camouflaged against their own backdrops |
| `T12` | TASK | "North" was a frame the robot could not perceive |
| `T13` | TASK | The fallen person was flung across the room by their own drop |
| `T14` | TASK | Blaze was calibrated to a robot that never thinks |
| `T15` | TASK | The fallen person was perched on a marker post -- and drift-only checks missed it twice |
| `T16` | TASK | `max_turns` was billing the harness's own filler as if the robot had thought |
| `T17` | TASK | The task-stack was never actually durable -- an adversarial reviewer found the real bug this project's own root-cause theory missed |
| `T18` | TASK | Re-verifying T17 by tracing real episodes surfaced a bigger, different bug |
| `T19` | TASK | The in-process sweep silently turn-capped 33 of 45 challenges tighter than their own stated budget |
| `T20` | TASK | The `released:` prompt text covered the wrong contradiction shape, and hardening it uncovered a real stale-fact bug |
| `T21` | TASK | Re-running the live baseline found two faults the validity gate structurally cannot see |
| `R4` | TASK | It declines correctly, but from across the room |
| `R5` | TASK | Six turns is not enough for a fetch, and it does not know that |
| `L1` | TASK | The robot was offered tools that cannot work here — my fault |
| `L1b` | TASK | What actually blocks 19 of 38 challenges is grasp vision |
| `L2` | TASK | The model connection drops mid-episode — theirs |
| `L3` | TASK | Navigation plans through walls it has never looked at — theirs |
| `L4` | TASK | The exported nav map claimed the world outside the building — mine |
| `L5` | TASK | "Assume perfect map" is an assumption this agent cannot use |
| `H21` | HARNESS | The benchmark's rooms were invisible to the robot's depth camera |
| `H22` | HARNESS | The live judge heard every remark as a claim: three remarks disqualified the right answer after them |
| `H23` | HARNESS | The last permitted move was charged to the budget and never driven |
| `H24` | HARNESS | The blaze map left the porch unknown, so Nav2 could not plan to where every blaze challenge ends |
| `H25` | HARNESS | The key-injecting shim relayed for anyone on the LAN |
| `H26` | HARNESS | The helpers took the first `innate-dev*` container, which could be another checkout's |
| `H27` | HARNESS | A failed map export ran the next map on the previous map |
| `H28` | HARNESS | A scripted challenge started from the web app asked a question nobody delivered |

### Found after the recorded runs (H21-H28)

An adversarial review of the merge-ready diff (GPT-6 Astra at its highest
reasoning setting, 8 Sep 2026) found eight more harness faults. All are fixed
in this tree, each with a test that fails on the old code. Three of them touch
the recorded numbers, and the direction of each is known:

- **H24 puts a ceiling under the live baseline.** Every blaze challenge ends
  on the porch, outside the south wall, and the exported map left all 627 of
  its cells unknown -- `allow_unknown: false` refuses to plan there. So on the
  live path `blaze_l1`..`l4` (two in category 2, two in category 3) could not
  be completed however well the robot played, so the 5/45 recorded on Aug 17
  had 41 achievable challenges, not 45. Blaze was re-run live on 8 Sep 2026 on
  the corrected map, with H21 and H22 fixed as well: `blaze_l1`..`l4` 0/4,
  0 of 11 goals, every brief and narrator line delivered, 287 model calls,
  $2.73 -- the same outcome as before, now on a map the robot could have
  completed. So the live baseline is 5/45 with all 45 achievable; the composite
  is the 41 episodes of the Aug 17 sweep plus these four. The in-process
  numbers were never affected (that path plans on the physics world, not the
  map).
- **H22 could only have lowered the live score.** The live judge posted every
  robot utterance as an `answer`, the structured channel where three distinct
  wrong values mean the robot is enumerating and disqualify the episode. A
  robot that narrates its progress three times and then answers correctly was
  disqualified on the live path and passed on the in-process one. Whether any
  recorded episode hit it is not recoverable from the results files, which
  keep the verdict and not the transcript; the fix is a `say` event on both
  paths, judged by the same whole-word rule.
- **H21's direction is not known.** The rooms' geoms were in the robot's own
  geom group, which the depth camera hides so the arm does not mark itself an
  obstacle -- so in every benchmark world the costmap's voxel layer saw props
  and no walls, while the lidar layer saw everything. The two live runs agree
  with each other on every category, which bounds what it can have changed,
  and the audit's certified-solvable count came from the in-process path.
- **H23 affects the in-process agents only**, and only when the turn budget
  ran out on a `forward` or `turn`: that move was recorded and never made.
- H25-H28 are operational: the shim's exposure, the wrong container with two
  stacks up, a stale map after a failed export, and cues the web app's runs
  never delivered. None of them can have produced a number.

### Bringing the live stack up from this fork (8 Sep 2026)

For the blaze re-run, on Docker Desktop with WSL, after merging upstream:

On Docker Desktop with WSL, bringing the stack up from this fork needed three
launcher knobs (8 Sep 2026): `INNATE_SIM_ALLOW_HOST_BIND=1`, because the bind
fallback fails closed (see the PR); `INNATE_SIM_ASSETS_IMAGE` naming
upstream's published assets image for the merged base
(`ghcr.io/innate-inc/innate-os-sim-assets:inputs-<hash>`, the hash being
`config.compute_assets_image_inputs_hash` over the upstream commit) when the
launcher refuses a partial geometry store -- a store installed before upstream
added the backrooms and intersection worlds is one; and, for the `--offline`
restarts `run_eval.sh` does, `INNATE_SIM_VIEWER_BUNDLE_IMAGE` naming the
`innate-os-sim-viewer-local:inputs-<hash>` bundle a first online `up` built,
since offline the launcher looks for the unpublished ghcr name. Two more
things: `innate-sim down` can leave the host world server holding ports 8799
and 8800 (kill `mars_sim_driver.world_server`), and a container write can
leave `data/` root-owned (chown it back, or `up` dies on `data/.last_map`).

### Closing the audit's two household items (6 Oct 2026)

`ENVIRONMENT_AUDIT.md` left two Household faults open after the six-world
refresh: the kitchen fetch named a person nobody spawned, and the fallen
person was dropped through the wall between the bedroom and living room.
Both are fixed, and Household has the refresh's finishes (per-room floors, a
ceiling, wall art, cabinet fronts, a cream delivery mat). Two things in the
fixing are worth keeping:

- **A checkout without the downloaded `sim/assets` measures the fallback
  box, not the scan, and says nothing.** The first fix put the person at
  (-2.0, 1.75) and measured 0.000 m of drift -- on the 0.5 x 1.7 m box that
  stands in when `casual_man.obj` is absent. On the scan the same drop slides
  0.17 m in the 1.5 s after a reset. Re-measured on the scan, it now lies at
  (-2.0, 1.2): 0.02 m, and the oracle scores 3/3.
  The prop's `rest_z` of 0.3 was not a height the body rests at (the scan
  rests at 0.126, the box at 0.157), so it is now 0.12. The tests pass on
  both, but only the scan is a real check of a position.
- **A visible destination has to satisfy the judge everywhere on it.** The
  brief says "put it down on the round cream mat in front of them", and the judge
  wants the mug within 1.0 m of Casey. The first mat's near rim was 1.05 m
  from Casey, so a mug set down on it could fail. The mat is smaller and
  further north now, and `test_household_scene.py` checks the whole disc
  against both halves of the judge.

### Every challenge through the robot's camera (6 Oct 2026)

The validity gate proves a challenge is solvable by a script that is handed
the coordinates. It cannot tell whether the brief makes sense to someone
looking at the scene. So all 45 were rendered from their real spawn and from
where a robot would stand, and each brief, narrator line and goal was read
against the pictures. Fixed:

- **Speech thrown away by goal order.** A spoken goal only hears speech made
  while it is the current goal. `counter_unspoken_request` judged the offer of
  help after a 5 s dwell at the customer, so offering help on arrival scored
  1/2; the offer now shares a phase with the dwell and must be made at the
  customer (`test_speech_on_arrival.py`). `counter_out_of_reach` judged the
  refusal only within 0.75 m of the teapot, where the teapot has left the
  camera frame, and the brief never said to go there: it now says so, at 1.0 m.
- **Briefs that licensed the "wrong" behaviour.** `counter_which_one` asked
  for "one of the cups" and failed a robot that brought one without asking;
  it now asks for "my cup". `household_tour` began "visit the living room"
  with the robot already standing in it, and a robot that took that as done
  scored 0/4; the brief now names the rug as the first stop.
  `counter_cafe_shift` sent the robot back to "the pad", which nothing had
  introduced; it is now "the green square by the door where you started".
- **Goals narrower than the brief.** `household_take_orders` judged "finally
  the bedroom" as a 0.85 m circle around an unmentioned corner marker (now
  the whole bedroom), and "check on them" at 0.9 m from the body's centre,
  which a robot kneeling at the head or feet missed (now 1.2 m).
- **Scenes that contradicted the brief.** Pantry's stocktake expects 3
  cartons on the shelves; an old cardboard delivery tray on the right shelf
  read as a 4th and is gone. Blaze's green wall band and skirting ran across
  the only exit, and its exit sign floated in the doorway. Rounds' "blue"
  delivery mat was teal. The workshop's "blue paint cans" fell back to red in
  the browser (and its amber oil cans to blue).
- **Graphics.** Ceilings in rounds, workshop and blaze (the robot looked up
  into black); closed doors where the counter's and pantry's doorways opened
  onto black (and let a robot drive out of the world); the counter's green
  wainscot, behind the green cup in "how many cups are on the counter", is
  white; z-fighting removed from the counter's mats and Blaze's kitchen
  fronts. Blaze's porch, a slab 0.5 m above the ground plane with open edges,
  has a low collidable wall on its three open sides and sky beyond it. A sky
  panel alone was tried first and broke the nav map: the exporter's virtual
  lidar saw it through the doorway and grew the map 312 free cells off the
  porch. The wall stops the lidar, and the robot, at the porch edge.

The regenerated nav maps (rounds, pantry, counter, blaze, and later household)
differ from the old ones where a door or Blaze's porch wall now stands or
household's posts no longer do, and otherwise only in frame: the exporter
sizes a map from every room geom's bounding sphere, so a ceiling or a split
trim strip moves the origin without changing what is drivable.

Four more, each a judge or scene that disagreed with its own brief:

- **Deliveries had to land upright.** The height bands were drawn from each
  item's upright rest (rest_z - 4 mm to + 15 mm), so a jar, carton, medicine
  bottle, document box or oil can set down on the right mat and tipped onto
  its side -- centre 25 mm up, 20 mm for the can -- failed, while every brief
  said "put it down". The book and the phone also rest stably on an edge,
  above their bands. Every band now runs from 4 mm under the item's lowest
  stable rest to 15 mm over its highest, measured by settling the item on its
  own mat upright, on each side and upside down (`test_delivery_bands.py`,
  which fails on the old bands in five worlds). Held, airborne and buried
  items are still out.
- **Blaze's flames reached the items before the deadline.** In `blaze_l3` a
  fire patch 0.2 m from the document box lit at 221 s while the kitchen was
  safe until 330 s; every level had one within 0.3 m of the medicine. The
  judge still accepted the item and the flames said otherwise. A patch within
  0.5 m of a rescue item now stays unlit until its room is lethal, in free
  play and in a run alike (`test_blaze_fire.py`).
- **Order words the judge did not enforce.** A delivered object stays
  delivered, so "the medicine first, then the phone" judged as two ordered
  placements passed in either order. A new `Not` predicate makes the first
  delivery count only while the second is not yet there: blaze 2-4, the
  stocktake's "in this order", and "the red one, then the blue one". Pantry
  restock's "leave the jar where it is" is now a goal.
- **Nobody at the counter.** "I'm at the middle seat" was spoken to an empty
  stool in `counter_unspoken_request` and `counter_which_one`. A customer now
  stands there: Alex's scan at 0.29 scale, since the café is built to the
  robot's size (0.21 m stools, a 1.4 m ceiling) and a life-size resident
  would stand through the roof.

### Every challenge played, not scripted (9 Oct 2026)

Rendering shows what the robot sees; it does not show what it is like to act
on it. So all 45 were played once each through `claude_bridge.py`, the
in-process pipeline the probes use: the brief, the narrator, the result of the
last action and one 640x480 frame per decision, nothing else. 38 passed. The
challenge file was read only after each run, to ask whether a failure was the
player's or the level's. The level's:

- **The harness, in every world.** A drive pressed against a door jamb, a
  bench corner or a can ground on for its full 14 s while contact swung the
  base 100-178 deg; the robot came out facing back the way it came, and this
  failed `workshop_bench_tour` and `blaze_l1` on time. A drive now stops once
  it gains under 2 cm in 1.5 s, or contact pushes it 25 deg off the heading it
  holds, and says so. The frame after `place` showed the item hanging at eye
  height, released and not yet fallen; the camera now waits 1.5 s. `pick`
  took the nearest object in a circle, which was once behind the robot and
  once a jar at the edge of the frame while the carton sat dead centre; it now
  takes the object the robot faces, within the camera's view and the arm's
  span, so a fallen person and a dog can no longer be "picked up". And it
  reported the prop's id, which answered the question: `workshop_gauge_040`
  ranks the oil cans by size, `pantry_jar_stray` names the misfiled jar. The
  gripper now reports a kind ("an oil can"). Earlier probe scores were taken
  with the old pick and are not directly comparable. (`test_brain_pick_place.py`)
- **A first frame taken mid-fall.** Setup props were dropped and the clock
  started in the same instant, so the fallen person in
  `household_take_orders` hung horizontal at sofa-back height in the robot's
  first frame. The scene now settles for 1 s before the run starts. Later in
  the same run the robot drove into the body and levered its legs up to
  camera height, where they caught it: a free convex hull has no flat face to
  lie on. A person on the floor is not something this robot can move, so the
  body is now placed, fixed, flat on its back, like the standing residents.
- **Order words, again.** `counter_second_order` and `counter_cafe_shift` say
  "when you've done that" and judged the two cups in either order; both now
  use `Not` as `counter_floor_two_orders` does.
- **Judges that contradicted their brief.** In `counter_carried_detail` the
  "back toward the counter" rect overlapped the right-seat circle, so the
  third leg latched with the robot still at the seat and the password question
  arrived early; it starts at y 0.8 now. `counter_cafe_shift`'s overheard
  "someone should shelve that teapot" had no judge; as in
  `counter_not_for_you`, the teapot must now stay put. `counter_follow_up`
  rejected "cobalt", which `counter_which_colour` accepts for the same cup.
  `blaze_l4` said "get what you can" and its narrator "if you want the study",
  then failed a robot that took the phone to be optional; the brief and
  narrator now say the phone has to come out (the photo is still the thing to
  leave), and `blaze_l2`'s "if the phone matters" became "get the phone now".
- **Directions the robot could not use.** Every blaze brief named an east and
  a west hall, and the robot has no compass; it starts facing a blank wall in
  a hall that looks the same both ways, so the kitchen was a coin toss and a
  wrong call cost most of the kitchen's window. The briefs now say which way
  is which from where the robot starts.
- **Graphics.** Blaze's single sky panel left black over the porch's side
  walls and over the house walls; the sky is now a closed box, which the
  observer's view leaves out like a ceiling and the nav map's bounds ignore.
  The blaze towels were a labelled pack on end -- glass blocks at 640x480,
  and it fell over at a touch -- and are now a low stack of folded towels.
  Household's doorways showed a strip of pale base floor that read, from the
  kitchen, as the riser of a raised living-room floor; they have thresholds.
  Its three 0.34 m station posts, named by no brief, looked like upturned
  tables (Casey stood on one) and the kitchen one stood collidable in the
  walkway; they are gone. "Sitting at the middle seat" is now "waiting", as
  the customer's scan stands, and the bridge's gate gaps are "openings", not
  "doors".

Played and left alone: the café's green mat sits behind the middle stool, so
the straight line from the spawn is blocked -- navigating round a stool is the
task. A jar within 0.3 m of the robot's front is pushed when it turns, and the
camera cannot see the floor within 0.5 m; both are the real robot's geometry,
and they make `pantry_restock`'s "leave the jar" a test of care rather than a
trap.

### The probe's robot has the robot's tools (10 Oct 2026)

The probe used to drive with eight harness actions -- turn N degrees, drive N
metres, grab what is in front, put it down 30 cm ahead, `answer`, `look`,
`finish`. MARS has none of those. On main, the brain sees a camera frame, a
status line and the events since its last turn, and acts by calling the
running agent's skills plus two built-ins; plain text is speech. A probe that
certifies "solvable" with a different action set certifies it for a different
robot. So `BrainAgent` (every in-process backend, the Claude bridge among
them) now offers exactly that:

- **The roster is read from main, not copied** (`innate_roster.py`):
  `intro_agent.get_skills()` -- the agent the brain initialiser picks by
  default -- with each skill's own description (its `guidelines()` or
  docstring, as the catalog builds it) and `execute()` signature, plus
  `go_to_point_in_view` and `wait` from `brain/tools.py`, under the brain's
  own system prompt. While a skill runs only `stop_current_skill` is offered,
  as `build_tools` does. `test_innate_roster.py` fails if main's roster gains
  a skill this does not run. `search_memory` is withheld, as the live run's
  roster gate withholds it: there is no memory tier in-process.
- **What stands in for the stack:** navigation plans on the static map and
  follows with Nav2's limits from `mars_nav` (0.45 m/s, 0.6 rad/s, its
  progress checker, and `nav_to_pose.xml`'s back-up-and-retry recovery);
  `go_to_point_in_view` is the brain's own `grounding.py` with the brain's
  sim camera config; `pick_any_object` finds the prompted object among the
  props the head camera can see (kind, colour and contents words -- never
  the prop id), picks off the FLOOR only, and folds to the skill's own
  `CARRY_ARM`, where both cameras show what it holds; `open_gripper` drops it
  from the claw. The brain also gets the wrist frame, as on the robot.
- **One timing simplification:** mid-skill the brain is woken when someone
  speaks, not every 5 s.
- The live run's roster (`prime_brain.sh`) enabled `move_straight`, which
  main has deleted; it now enables the same default-agent roster.

**All 45 played again, blind, with these tools: 45 of 45 passed** (73
episodes; a level was replayed whenever a fix touched it, and 13 of the 73
failed). The failures, each with its cause:

| failures | kind | cause |
|---|---|---|
| 6 | HARNESS | a released item wedged in the never-opening sim claw (blaze_l2's phone rode four `open_gripper` calls); a turn to the goal heading that a door jamb blocked "settled" for four minutes with no event; a follower with no recovery froze against blaze's kitchen jamb (three players running); items released from the prop's 0.19 m shelf-drop height landed on their side and rolled under the shelf |
| 3 | TASK | the judges below |
| 4 | AGENT | the player's own call: a refusal made from 1.5 m away, a jar knocked over by a careless approach, time lost in blaze_l3 |

Eleven levels that only drive and speak (bridge, gallery's ring and plinth
levels, household's tour and orders, two counter questions, the jar count)
passed before the carry and recovery fixes, which they do not exercise, and
were not replayed.

Judges and briefs the replays caught -- each fixed and re-gated:

- **A question answered before it was asked.** `counter_follow_up` latched
  both goals on "three cups: red, blue and green", before its follow-up was
  ever spoken. A goal that gates a cue now ends judging until the cue has
  fired (`test_follow_up_is_asked.py`); the deaf oracle pauses a second.
- **Order judged at the wrong time.** Seven goals wrote "the carton, then the
  jar" as AllOf(carton there, Not(jar there)), judged when the goal's turn
  came. In `pantry_stocktake` a robot that miscounted, delivered both in the
  right order and then corrected the count could never pass. `Before`
  remembers which happened first (`test_order_is_when_it_happened.py`).
- **A delivery credited after it rolled away.** Goals latch; a jar that lay
  on its mat for the 0.75 s hold and then rolled off still scored 5/5. The
  stocktake's last goal now checks both items are still on their mats.
- **Honest answers the regexes missed.** `counter_out_of_reach` accepted
  "can't bring" but not "cannot bring", nor "above my arm reach"
  (`test_out_of_reach_phrasings.py`); `counter_unspoken_request`, whose
  docstring promises any service-shaped offer, rejected "Is there something I
  can bring you?".
- **Briefs that disagreed with their judges.** "Wait by the right seat"
  measured from the mat 0.42 m in front of the seat, so a robot waiting
  beside the stool failed; "come back toward the counter" needed the robot
  at it; four briefs said "within half a metre" (or "one metre") of goals
  judged at 0.6 m (1.2 m).
- **Graphics.** The bridge's "numbered gates" carried pips -- gate 1 a single
  dark square that read as a smudge -- and now show numerals. The cafe
  customer stood behind the middle stool, legs through it from the robot's
  height; she stands beside it. A stray brass strip from the pantry export
  lay across the delivery pad like a plank. The nav maps are unchanged.

Played and left alone, because the real stack does the same: Nav2's first
goal-checker tier (`dynamic_goal_checker.cpp`) accepts a goal within 0.25 m
and 9 deg at once, so a 10 cm nudge reports "Reached" without moving (the
probe settles to 0.10 m, tighter than the robot); `navigate_to_position`
reports the goal it was given, in the frame it was given; the arm stays in
its carry pose after `open_gripper`, in the lower right of the head camera.

### Every level against the environment audit's rubric (11 Oct 2026)

`ENVIRONMENT_AUDIT.md` names four ways a level fails a human: it needs a skill
the robot lacks, it names something the scene does not show, it judges a goal
the robot is never told, or it accepts an outcome that is not physically real.
Each of the 45 levels was started, settled for 1.5 s and dumped -- brief,
cues, every goal's predicate, every prop's pose, deep contacts, the head
camera at four headings and an overhead -- and four reviewers who had not
seen the levels judged each against those four tests plus "does it make
sense". Every prop a setup drops is in the scene, none overlaps anything by
more than 2 mm, and every object, person and landmark a brief names is in
it and visible; the ring tour's cans stand where its brief puts them. No
level was found unpassable. What they did find, all fixed:

- **The spoken-answer judge failed honest answers.** Its hedge words were
  substrings, so "or" in "color" and "for", and "no" in "No problem -- there
  are three", disowned the answer; its scope ran to the last comma, so the
  brief's own exclusion said back ("Not counting the red mugs there are
  eight") negated the count; only the first mention of the number was
  checked; and "I think" counted as a hedge. Hit: read_the_pass,
  which_colour, follow_up, carried_detail, count_ring, count_doors, both
  jar counts. Now whole words, the last three words of the clause, every
  mention (`test_answer_semantics.py`).
- **Judges that passed the wrong thing.** `pantry_misfiled` accepted the bare
  word "jar", so "On my way to the jar shelves" -- or the brief read back --
  passed at t=0; it now needs the item named. `counter_which_one` took any
  list of colours as asking ("Picking up the red cup, then the blue one"); it
  now needs a question. `gallery_fetch_mug` judged a 0.45 m circle with no
  height band and no hold, so it latched with the mug still in the claw; it
  now uses the suite's delivery rule.
- **Tests that could not fail.** `counter_not_for_you` and
  `counter_cafe_shift` tempted the robot towards a teapot on the counter,
  which a floor-only pick cannot lift, so "left alone" always held. The
  overheard errand is now a jam jar on the floor.
- **Speech thrown away for being early.** Speech heard before its goal is
  discarded and the brain is told never to repeat itself, so an admission
  made on the way to the shelf (`counter_out_of_reach`) or a count given
  before arriving (`pantry_count_jars`, `workshop_count_benches`) was lost
  for good. Arrival and answer are one phase now; the admission still has to
  be made within 1.5 m of the shelf. The out-of-reach and offer-of-help
  patterns gained the honest phrasings they missed ("not able to reach",
  "only pick things up off the floor", "How can I assist you?").
- **Judges measured from the wrong place.** `counter_unspoken_request`'s
  circle sat on the mat, 0.57 m from the customer it is about;
  `counter_carried_detail` put "the seat" on the mats while `route_change`
  puts it on the stools; `workshop_bench_tour`'s 60 cm meant parking within
  14 cm of a bench, inside Nav2's 0.3 m inflation (now 75 cm, still not
  overlapping); `pantry_count_jars` judged a 0.85 m circle on the middle of
  a 2.2 m shelf.
- **A required item placed where the skill must hit it.**
  `pantry_restock`'s "leave the jar" jar sat 7 cm off the line from the
  door to the carton, where the pick parks the base; it is on the far side
  now.
- **Words that disagreed with the level.** blaze_l2's opening cue said "if
  you're getting the medicine at all" of a medicine the judge requires;
  blaze_l3's said "you won't get long" of a 5:30 deadline; blaze_l4's
  "last chance for the phone" came 10 s before the hall closes and a pick
  takes 20-30 s. `counter_change_of_mind`'s correction fired at 0.45 m, with
  the red cup already in the claw (now 0.7 m). The leftover "never claimed
  it was out of reach" goals in both within-reach levels only failed
  harmless narration. Briefs now say "the higher one" (one mug has no
  plinth), "go to the dog" (the judge measures the dog), "the bedside
  table" (where the book is), "tell me the total" (stocktake), "waiting"
  (the customer does not wave), and the ring tour no longer gives away the
  count asked in the same room.
- **Distractors.** The pantry's shelf tickets were 11 x 5 cm and pale, the
  size of the cartons being counted; they are price tags now. The smallest
  oil can stood 0.42 m from a terracotta pot; the row moved 0.4 m east.

Left as they are: the red door's 0.40 m of free map against a 0.33 m
footprint is `rounds_narrow_door`'s deliberate control, and the players got
through it with the robot's tools; the photo on blaze_l4's bedside table is
the intended thing to leave behind (the brief now says where it is).

Checked after the fixes: every world passed the validity gate again (the
oracle solves each level, the random agent fails it; the café's three floor
levels need the arm and rest on the probe), and the ten levels whose judge or
scene changed in a way that could make them harder -- which_one,
not_for_you, cafe_shift, out_of_reach, unspoken_request, carried_detail,
fetch_mug, misfiled, restock, fetch_gauge -- were each played again blind with
the robot's own tools, and all ten passed. The probe's 45/45 above and the
scores table were measured on the 10 Oct levels and were not re-run.


## HARNESS or AGENT -- embodiment is a constraint, not a third verdict

The take-home brief is explicit about what this benchmark exists to
measure: innate.bot's AGENT (MARS) -- observation, instruction-following,
long-horizon planning -- not the specific simulated hardware it happens
to be deployed on today. There are exactly two verdicts a failure can
reach, not three:

- **HARNESS** -- a bug or unfairness in the benchmark itself: a rubric
  demanding more than its brief, a judge circle authored in the wrong
  place, a color indistinguishable from its own backdrop, a scheduling
  bug that starves any agent regardless of how well it reasons, OR a time
  / turn budget that does not yet account for a real physical cost the
  robot pays. Wrong for every agent equally, found and fixed, never
  scored against anyone. T9-T13, T15, and T16's turn-budget half are all
  this bucket.
- **AGENT** -- everything else. The robot's fixed physical properties
  (0.34 m arm reach, no reverse drive, real door-jamb collision physics,
  camera height/FOV) are not a blame bucket of their own -- they are
  DESIGN CONSTANTS the agent is handed, identical for every agent
  deployed on this body, and the agent's entire job is to reason well
  within them. A real physical cost is never, by itself, an excuse: T14's
  door-jamb contact costs up to 154 degrees of drift and ~22 s *only if
  triggered*, and the oracle -- which navigates the same real physics with
  perfect precision -- passes single-item evacuation clean, proving the
  cost is avoidable in that case, not fixed. So the correct move is never
  "embodiment made this hard, partial credit to the harness" -- it is:
  first, HARNESS calibrates its budgets to be fair given the real,
  disclosed cost (T14 already did this for both forms, by measurement);
  and only once that fairness is established does whatever residual
  failure remains become AGENT territory -- specifically the agent's
  navigation precision, since a more careful approach avoids the graze,
  same physics and all. "Embodiment" explains *why* the stakes are high.
  It never splits the verdict.

Two honest limits on this, stated rather than smoothed over:

1. **"Clock-fair" is not always the same claim as "proven 100% solvable."**
   T14's single-item blaze form has both: a clean, reproduces-to-the-
   second oracle pass. The full multi-item form only has the first --
   a budget matched to measured real costs -- and its own text records
   coming within one blocked drive of finishing, twice, not finishing.
   Calling that row's residual AGENT is the best current attribution, not
   a closed case; the honest label is in the scoreboard below.

A second supposed exception -- "the Bridge has real randomness baked in,
a single failed episode there is nobody's fault" -- was drafted into an
earlier version of this section and did not survive checking the actual
challenge source. It does not: `bridge_three`/`bridge_five`'s route is a
FIXED sequence stated in the brief, not a dice roll (see known-limit #3,
corrected the same way). There is no genuine chance-based exception in
this benchmark. That correction is left visible rather than quietly
edited away, because catching your own claim before it ships is the same
discipline this whole ledger asks of every other finding.

With that one named, the rule holds everywhere else in this ledger:
nothing should be read as "the robot's fault" and left there. A failure
is either the benchmark's fault (fix it) or the agent's (real signal) --
never an unexaminable draw between the two.

---


## The probe scoreboard

Superseded on 10 Oct 2026 by the replay with the robot's own tools (45 of 45,
above). Kept for the record of what the eight-action probe found:

92 episodes, 16 agents, every challenge attempted at least once on its final
definition. **36 of 45 passed** — the solvability certificate. The nine
non-passes, each with its verdict:

| challenge | best | kind | verdict |
|---|---|---|---|
| blaze_l2 / l3 / l4 | 1/3 | AGENT (best current attribution, not fully closed) | HARNESS side calibrated against measured real costs (T14); but only the SINGLE-item form has a clean, reproduces-to-the-second proof of 100% achievability -- the full multi-item form is "clock-fair" by measurement, not proven solvable by perfect play, so it is honest to call the residual AGENT navigation precision, not honest to call the case closed |
| household_take_orders | 2/3 | HARNESS (likely) | person+dog legs proven; bedroom lost to white-on-white search (found by an earlier probe) -- reads as T11's camouflage pattern (a perfect-perception agent fails too), not confirmed with a dedicated finding entry |
| household_tour | 3/4 | HARNESS (likely) | same bedroom; bathroom leg proven |
| pantry_count_jars | 1/2 | AGENT | the misfiled-jar trap caught two strong agents — the task working as designed: real signal about carefulness, not an apparatus problem |
| pantry_stocktake | 0/5 | AGENT + HARNESS | same trap at goal 0 (agent); "ordered latching then hides 3 completed subtasks" is a genuine judge-sequencing bug (harness) layered on top -- two separate defects in one row, not a split verdict on one |
| rounds_all_doors | 0/4 | AGENT | agent mistook a door post for the room; narrow_door (a control) proves the judge gives entry credit correctly, so this is a perception/identification miss, not a scoring one |
| rounds_deliver_book | 2/3 | AGENT | book found and picked (post-fix, HARNESS side already resolved); delivered to the wrong landmark -- a navigation/identification error |

Where the live robot fails one of the 36, the failure is downstream of the
system under test. Where it fails one of the nine, the `kind` column says
whether that's benchmark unfairness (HARNESS, always fixed on sight, and
that includes budgets that had not yet been calibrated fair against a
real physical cost) or the actual MARS decision-making falling short
(AGENT) -- the only one the take-home is asking to measure, and the only
verdict a fixed hardware property is ever allowed to resolve to once the
harness has done its job.

---


## The two live runs, side by side

Same robot, same brain, same maps. Between them: every fault in this ledger
found and fixed, 26 of 45 challenge definitions corrected, and a strong-agent
probe certifying 36 of 45 solvable (92 episodes).

| | Aug 16 (pre-audit) | Aug 17 (final benchmark) |
|---|---|---|
| challenges | 4/45 | 5/45 |
| goals | 18/121 | 22/121 |

The Aug 17 column is a composite made honest the hard way: the first pass
silently skipped household, bridge and blaze (a wedged Docker daemon broke
the container's network mid-run; the runner's auth pre-flight refused each
map and the sweep still printed "done"), and rounds' two long errands ran
with their briefs undelivered after the priming scripts timed out. A patch
run the same night re-executed all four bundles fresh -- landing, after
every repair, on the identical headline totals. The lesson is recorded here
because it is the project's oldest one wearing a new coat: the REPORTING
pipeline can also fail quietly, so the final aggregation asserts it holds
exactly 45 same-day episodes before it prints a number. Blaze's four were
re-run on 8 Sep 2026 on the corrected nav map (H24 below): 0/4 again, so the
column's totals stand, over 45 achievable challenges rather than 41.
| observation+conversation | 2/13 | 3/13 |
| simple instruction | 2/17 | 2/17 |
| long-horizon | 0/15 | 0/15 |
| cost | $7.60 | $6.05 |

The near-identical totals are the finding. The audit moved the *achievable*
score from unknown to a certified 36/45 while the robot's score barely moved
— so the 31-challenge gap is now attributable to the system under test, not
the apparatus. New passes are the two conversation probes built this session
(follow_up 2/2 in 14 s, unspoken_request 2/2): the brain converses better
than it does anything else. The stable zeros are exploration (no map, no
semantic navigation — its own words: "I don't have a map ... but I can move
around locally"), manipulation approach, and long-horizon thread-keeping
(0/15 in both runs, with first-goal partials throughout). Run-to-run variance
is real: route_change passed on Aug 16 and not on Aug 17; single-run results
on this system are indicative, not precise.

---


## Known limits of this benchmark

Stated here rather than discovered later.

1. **An oracle pass is not proof that the ROBOT'S action set can do it.**
   The scripted oracle drives with A* waypoints and, for carry tasks,
   `put` teleports the held prop to its destination -- `oracles.py` says so
   in its own header, and `planner_agent.py` calls `mars.drop_prop_at`
   rather than the arm. So VALID means "the world permits this outcome and
   chance cannot reach it", NOT "the robot's tools can complete it". The
   probe is the second claim: an agent with MARS's own tools passed all 45.
   What stands in for Nav2, Gemini and the arm in that probe is stated in
   `innate_roster.py`; the floor-only pick and the drop from the claw follow
   the skills, but the object finder is a matcher over visible props, not a
   vision model.

2. **No ASR.** Narrator lines arrive as text. The real stack's speech front
   end is upstream of everything here and is not exercised by any challenge.

3. **`bridge_stutter` is not a clean disfluency control.** It was built as the
   disfluent twin of `bridge_five` -- same map, same route, same failure rule
   -- so that the pair would price disfluency. It also changes PERSISTENCE,
   which was not intended. `bridge_five` puts its route in the brief, and the
   observation re-emits the brief every turn, so there the route is permanent.
   `bridge_stutter` withholds it and delivers it as script cues, which the
   agent loop drains rather than copies, so each line is visible for exactly
   one turn. Checked against a real episode: the route appears in turns 2-3
   and is absent from turn 4 onward, on a run of about 21 turns. Every shipped
   backend builds a fresh prompt per turn, so by the time the gates are
   reached the route is structurally unavailable. Its score therefore prices
   cross-turn memory at least as much as disfluency, and the two cannot be
   separated from the number as it stands.

4. **The blind control is not a vision result.** `CodexBackend` is text-only
   because the Codex CLI takes no images. Its scores answer "what is reachable
   from the brief alone" and must not be quoted as perception numbers.

5. **"1 in 8" / "1 in 32" on the Bridge is a guessing floor, not environmental
   randomness -- corrected after actually checking the challenge source.**
   `bridge_three`/`bridge_five`'s left/right route (`ROUTE = (...)` in each
   challenge file) is a FIXED sequence, identical every episode, stated
   outright in the brief ("go right, then left, then right"). There is no
   dice roll in the world. "1 in 8" (2^-3) and "1 in 32" (2^-5) describe what
   a non-comprehending agent blind-guessing at each independent binary gate
   would achieve by luck -- a floor under a meaningless PASS, not a ceiling
   a working agent should expect to bump into. A single PASS is still weak
   evidence on its own (it could be that lucky guesser). A single FAIL, and
   especially a REPEATED, IDENTICALLY-REASONED fail, is not weak evidence at
   all -- it is exactly the AGENT-capability signal this benchmark exists to
   surface (see the repeat-run results in NEMOTRON_STACK_RESULTS.md, which
   found consistent, repeatable failures on both, not scattered chance
   losses).

6. **RHAE has no human baseline.** ARC-AGI-3's efficiency score is
   `min(1, h/a)²` where `h` is the second-best *human's* action count. No human
   data has been collected, so the derived plan's step count stands in. It is a
   score against a reference plan, not against a person, and is labelled as such.

7. **`fail_if` is evaluated at 10 Hz.** A robot moving faster than ~2 m/s could
   in principle cross an 18 cm elimination band between ticks. Nothing in this
   sim moves that fast (V_MAX is 0.30 m/s), but the band width is a function of
   speed and would need revisiting if it changed.

8. **The oracle proves solvability, not sanity.** It is deaf by construction and
   plans straight to the final state. It cannot tell you a challenge is
   confusing, ambiguous, or badly worded — only that some agent could satisfy
   the goals. Every challenge here still needs a human to read it.

9. **Most rooms have no ceiling geom (counter, bridge and pantry carry a
   decor lid), and their walls are separate boxes, not a sealed shell.** At most camera angles this is invisible (the background
   above wall-height is a flat black "sky," same as any open-air view). At a
   narrow band of oblique angles near a wall corner, the seam between two
   non-abutting wall pieces can let that black background show through as a
   small rectangular patch — confirmed harmless (a second-pass fairness probe
   flagged one, on `blaze_l4`; it was absent from the immediately preceding
   turn's frame, i.e. present only from that one grazing angle) but real, and
   worth knowing before reading a black patch in a frame as a missing object
   or an occlusion. A true fix means giving every room a real sealed shell,
   which — like the rooms themselves — is generator output ("do not
   hand-edit: rebuild the map and re-run the exporter"), so it is disclosed
   here rather than patched by hand.
