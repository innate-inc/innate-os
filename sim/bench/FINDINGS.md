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
the robot's own action set and far more time, certified 36 of 45 solvable over
92 episodes.

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

The gap is the robot's, not the benchmark's: 36 of 45 are certified solvable,
5 were passed. Long-horizon is a wall rather than a slope -- 0/15 for both,
in two independent live runs.

## Every fault found, and whose it was

48 entries. One line each; the full account of any of them is in
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
exactly 45 same-day episodes before it prints a number.
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

1. **No ASR.** Narrator lines arrive as text. `bridge_stutter` measures what the
   language model does with disfluent *text*; the real stack's speech front end
   is upstream of everything here and is not exercised by any challenge.

2. **The blind control is not a vision result.** `CodexBackend` is text-only
   because the Codex CLI takes no images. Its scores answer "what is reachable
   from the brief alone" and must not be quoted as perception numbers.

3. **"1 in 8" / "1 in 32" on the Bridge is a guessing floor, not environmental
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

4. **RHAE has no human baseline.** ARC-AGI-3's efficiency score is
   `min(1, h/a)²` where `h` is the second-best *human's* action count. No human
   data has been collected, so the derived plan's step count stands in. It is a
   score against a reference plan, not against a person, and is labelled as such.

5. **`fail_if` is evaluated at 10 Hz.** A robot moving faster than ~2 m/s could
   in principle cross an 18 cm elimination band between ticks. Nothing in this
   sim moves that fast (V_MAX is 0.30 m/s), but the band width is a function of
   speed and would need revisiting if it changed.

6. **The oracle proves solvability, not sanity.** It is deaf by construction and
   plans straight to the final state. It cannot tell you a challenge is
   confusing, ambiguous, or badly worded — only that some agent could satisfy
   the goals. Every challenge here still needs a human to read it.

7. **Rooms have no ceiling geom, and their walls are separate boxes, not a
   sealed shell.** At most camera angles this is invisible (the background
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
