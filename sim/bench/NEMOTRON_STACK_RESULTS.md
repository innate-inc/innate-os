# NemotronStackBackend: results

Full 45-challenge sweep, `--agents brain:nemotron_stack`, in-process (no
Docker/ROS2), each challenge run to its own stated `time_limit_s`. See
AGENT_SPEC.md for the architecture and what's real vs substituted.

## Numbers

| | Aug 16 live (pre-audit) | Aug 17 live (final, audited) | NemotronStackBackend (original) | NemotronStackBackend (T19-corrected) |
|---|---|---|---|---|
| Challenges passed | 4/45 | 5/45 | 5/45 | **6/45** |
| Goals | 18/121 | 22/121 | 32/121 | **33/121** |
| cat1 -- observation/conversation | 2/13 | 3/13 | 3/13 | 3/13 |
| cat2 -- simple instruction | 2/17 | 2/17 | 2/17 | 3/17 |
| cat3 -- long-horizon | 0/15 | 0/15 | 0/15 | 0/15 |

The last column corrects a harness bug found and fixed after the
original column was recorded -- see FINDINGS.md T19 and "T19-corrected
numbers" below for exactly what changed, what was re-run, and what was
not. Read the "original" column as historical record of what was
measured, and the "T19-corrected" column as the honest current number.

**Read this as: same number of full closes, meaningfully more partial
progress.** Goal credit is up 45% over the audited live baseline (32 vs 22)
while pass count is flat. That is not noise -- it means the reachability
tool and task-stack are doing real work getting further into each chain
(more picks land, more corrections survive, more of a multi-step errand gets
attempted), but something is still capping most episodes short of the final
goal. Below is what that something is, by category, not glossed over.

## METHODOLOGY CAVEAT, stated plainly

This is NOT a controlled apples-to-apples comparison with the live numbers,
and reporting it as one would be exactly the kind of unreproducible number
this benchmark's whole discipline exists to catch:

- The live runs went through the ACTUAL Docker/ROS2 stack -- real skill
  servers, real nav2, real rosbridge round-trips.
- NemotronStackBackend runs IN-PROCESS against the harness's own primitive
  action set (turn/forward/pick/place/say/answer/look/finish) -- the same
  judge, the same challenge definitions, the same simulator physics, but a
  different execution substrate.

What IS controlled: the judge (`ChallengeEngine`), the challenges
(identical `Challenge`/`Goal`/`Predicate` objects), and the simulator
(identical `VirtualMars`). What is NOT controlled: the skill-execution path.
Treat this as "does the architecture make progress against the same
challenges the harness measures," not "is this what the real robot would
score" -- that second question needs the real NemotronLabs model wired into
the actual brain_client, which this session could not do (no NIM/Nemotron
API access, no physical Jetson).

**Two of the 45 rows in every column above were each a single episode, not
a rate -- and an earlier draft of this note called that "a roll of dice."
Checking the actual challenge source (`bridge_three`/`bridge_five`'s
`ROUTE = (...)`) shows that's wrong: the left/right sequence is FIXED,
stated in the brief, identical every episode. There is no dice roll in
the world.** "1 in 8" / "1 in 32" is what a non-comprehending agent
blind-guessing at each independent gate would clear by luck, not
something a working agent should expect to sometimes fail by chance. So a
single episode here was never weak evidence the way a genuinely
probabilistic trial would be -- it just wasn't enough episodes to see
whether a failure is consistent (real capability signal) or one-off
(could be anything). `nemotron_stack_results.json` (branch benchmark-evidence) confirms this column
ran each exactly once; the Aug 16 snapshot
(`results/eval/bridge_cat2.json`/`bridge_cat3.json`) shows the baseline
did too.

Repeats were run to settle this properly rather than leave it a single
point (per-episode records in `results/_repeat_bridge_three.json` /
`results/_repeat_bridge_five.json` on the `benchmark-evidence` branch; the
throwaway driver script was not
kept). In-process, same backend --
NemotronStackBackend, our new agent, not innate's own base stack -- same
harness: 6 fresh episodes each of `bridge_three` and `bridge_five`.
**Result: 0/6 and 0/6 -- 0/12 total, 11 of 12 with the identical
`fail_reason` ("went through the wrong door"), the 12th a turn-budget
exhaustion from excessive backtracking.** Real per-episode cost came in
far above the single historical run this table was built from (300-500s
typical vs the ~70-90s one lucky-fast run suggested), so the repeat count
was capped at 6 each rather than the original 30-each plan -- but because
the route never varies, 0/6 twice with a near-uniform failure reason is
already conclusive, not a small sample waiting on more data. This is a
real, repeatable capability gap in the new agent specifically (not
chance, not innate's base stack): it cannot reliably hold a 3-to-5-step
instruction it was given once across the several unrelated turns of
driving/looking that follow. The likely mechanism -- consistent with
finding 1 below ("Post-action coherence loss") -- is the same task-stack losing the thread that lost
`counter_within_reach`'s already-completed pick; here it is losing a
plan instead of a completed action.

## What actually capped most episodes

Read from the full per-episode trace (`nemotron_stack_results.json`,
reason field mostly "agent finished its plan"). **Correction, made after
FINDINGS.md T19: that reason field does NOT mean "the model decided it
was done" as this section originally claimed here.** `runner.py`'s
`agent.done` is true on either an explicit `finish` action OR
`turns >= max_turns`, and both report through the identical default
string -- so a genuinely still-working agent that ran out of turns and
one that actually quit are indistinguishable from this field alone.
Checked directly, not assumed: **15 of the 45 episodes in this very
results file ended at exactly `turns == 40`**, the harness's flat
class-default turn cap that `main.py` was silently applying to every
challenge regardless of its own `time_limit_s` (see T19 -- fixed). A
targeted re-run of the 14 highest-exposure challenges at their corrected,
larger caps (below) found 9 of 14 STILL hit their new cap exactly. Item
0, added on top of the original three causes, not folded into them:

0. **The turn cap itself, for a meaningful share of episodes.** Not a
   model limitation -- a benchmark-configuration bug, wrong for every
   agent that ever ran through `main.py`, unrelated to anything about
   this architecture. See "T19-corrected numbers" below for the actual
   measured effect of fixing it (modest, not dramatic, and reported
   precisely rather than left at a theoretical worst case).

1. **Post-action coherence loss.** The clearest example:
   `counter_within_reach` -- the agent DID grasp the jar (verified: "picked
   up the counter_jar_jam (0.20 m away)"), DID carry it toward the counter,
   DID place it -- and then, two turns later, said "I cannot reach the jar
   because it is at a height of 0.46 metres" about an object it had already
   delivered. This is a genuine limitation of the substitution, not the
   architecture: a real full-duplex model holds continuous audio-grounded
   state across the whole exchange; a turn-based Gemini-flash call re-derives
   its situation each time from a compact task-stack, and here it lost the
   thread despite the task-stack existing specifically to prevent that. This
   is disclosed rather than hidden because a stronger conversational core
   (the REAL NemotronLabs model, or a model with better long-context
   coherence than flash) would plausibly close a meaningful share of the
   "picked correctly, then contradicted itself" episodes -- this session
   cannot test that claim, so it is reported as a hypothesis, not a result.

2. **Turn-based navigation still pays the no-reverse/contact-drift tax.**
   Same mechanic FINDINGS.md documents for the live robot and every probe
   agent: a blocked drive can silently rotate the base up to ~100+ degrees
   and there is no backoff action, so a bad first contact angle at a
   doorway or a cluttered prop can eat a large share of a turn budget before
   the actual task starts. This is an ACTION-SET limitation (the harness's
   fixed 8-action menu has no reverse), not something the new agent
   architecture changes -- a real deployment would want to add a signed
   drive primitive regardless of which brain sits behind it.

3. **Height estimation without a depth sensor is the honestly-flagged weak
   link.** AGENT_SPEC.md's implementation-reality table called this out
   before running anything: a single monocular frame cannot give a reliable
   height-above-floor number without a support-to-floor anchor point in
   view. The pinhole-geometry prompt fix (this session) measurably improved
   it -- both the reachable jar (0.14-0.17 m, correctly under the arm
   ceiling) and the genuinely out-of-reach teapot (0.44 m, correctly over
   it) were judged correctly in isolation -- but the estimate still varies
   turn to turn for the same physical object, which is a real sensor-noise
   problem a depth camera would remove and a single RGB frame cannot.

## T19-corrected numbers: what fixing the turn cap actually changed

An adversarial review of the T19 fix computed which episodes in the
original sweep carry the `turns==40` + ambiguous-reason signature: 15,
and named the 14 (one of the 15, `bridge_stutter`, plus 13 others spread
across counter/pantry/rounds/workshop/household) as cheap and
high-value to re-run at their corrected caps rather than leaving the
fix's real impact as a theoretical bound. Re-run, one episode each, same
backend, same harness, only the turn cap changed:

| challenge | before (cap=40) | after (corrected cap) | new cap | Δ |
|---|---|---|---|---|
| counter_floor_two_orders | 1/3 | 0/3 | 66 (hit) | -1 |
| counter_carried_detail | 1/4 | 1/4 | 66 (31 used) | 0 |
| counter_floor_within_reach | 0/3 | 0/3 | 46 (hit) | 0 |
| counter_not_for_you | 1/3 | **3/3 PASS** | 46 (16 used) | **+2, new pass** |
| counter_three_orders | 0/3 | 0/3 | 100 (9 used) | 0 |
| counter_which_one | 0/2 | 0/2 | 46 (37 used) | 0 |
| pantry_stocktake | 0/5 | 0/5 | 80 (hit) | 0 |
| workshop_count_benches | 1/2 | 1/2 | 46 (hit) | 0 |
| rounds_count_doors | 1/2 | 1/2 | 46 (hit) | 0 |
| rounds_all_doors | 1/4 | 1/4 | 80 (hit) | 0 |
| rounds_find_bathroom | 0/1 | 0/1 | 46 (hit) | 0 |
| rounds_deliver_book | 0/3 | 0/3 | 100 (hit) | 0 |
| household_fetch_mug | 1/2 | 0/2 | 100 (hit) | -1 |
| bridge_stutter | 1/5 | 2/5 | 46 (18 used) | +1 |

**Net: +1 pass (0 to 1 of these 14), +1 goal (8 to 9 total).** Spliced
into the other 31 challenges (unaffected by this fix -- either they
already had cap ≤ 40 headroom, like `bridge_three`, or were not among the
14 re-run): corrected totals are **6/45 passed, 33/121 goals** -- the
numbers in the table at the top of this document.

**This is a real, honestly modest result, not the dramatic one a
theoretical bound would suggest.** The review that identified these 14
also computed a maximum-swing bound if every one flipped from fail to
pass (5/45 -> 19/45): that bound was never a prediction, and the actual
measured result -- one new pass, two other goals gained, two goals LOST
despite more turns being available (`counter_floor_two_orders`,
`household_fetch_mug` -- plausibly model non-determinism, or the "Turns
left: N" prompt line changing from turn 0 on every re-run episode, not
only at the tail, which the fix does not control for) -- is far smaller.
**9 of the 14 still hit their new, larger cap exactly.** The turn cap
being wrong was real and worth fixing; it was not, for most of these
challenges, the thing standing between them and completion. Most of them
needed the corrected budget AND still were not enough turns, which is a
different, more honest conclusion than "the bug was hiding a lot of
capability."

## What worked outright

- `counter_read_the_pass`, `counter_which_colour` -- identical to both live
  runs: pure perception, no locomotion, passes cleanly.
- `pantry_misfiled` 2/2, `gallery_fetch_mug` 2/2, `workshop_occlusion` 1/1 --
  three NEW passes the live robot has never achieved, all requiring either
  a full fetch-deliver chain or a changed-viewpoint search, both squarely
  in the failure classes AGENT_SPEC.md targeted.
- `gallery_ring_tour` 3/4, `counter_floor_serve` 2/3 -- one goal short of a
  full pass on tasks the live robot has never scored above 1/4 or 1/3 on.

## What this does and doesn't prove

Does: the three general mechanisms (reachability tool, task-stack, bounded
tool-calling) produce measurably more forward progress on the SAME
challenges, through the SAME judge, without any challenge-specific tuning
(see AGENT_SPEC.md's no-overfitting section and its grep-verified claim).

Does not: prove what the REAL NemotronLabs VoiceChat + a real Jetson would
score. That number does not exist and this report will not pretend it does.

## T17 re-verification: did fixing the task-stack actually change anything?

FINDINGS.md's T17 fixes `_TaskStack.apply()`'s destructive goals/constraints
replace and adds a mechanical `released:` fact checkpoint, after three
rounds of adversarial review (see that entry for the full process -- two
of the three rounds found real, confirmed bugs in the fix itself before
it was trusted). Once landed, the same three challenges that motivated
the fix were re-run at the same n=6 sample size as the pre-fix baseline,
same backend, same harness, for a direct before/after:

| | pre-fix (0 pass) | post-fix |
|---|---|---|
| `bridge_three` | 0/6, 5/6 "went through the wrong door" | 0/6, 4/6 same reason, 1/6 "agent finished its plan" |
| `bridge_five` | 0/6, 5/6 "went through the wrong door" | 0/6, 4/6 same reason, 2/6 "agent finished its plan" |
| `counter_within_reach` | 1 known pre-fix failure (explicit false "cannot reach it" claim after already delivering) -- no repeat baseline exists for this challenge specifically | **1/6 full clean pass (2/2)** -- picked, delivered, never falsely claimed unreachable; 5/6 failed 0/2 via "agent finished its plan" |

**Bridge: no measurable change.** 0/12 before, 0/12 after, same dominant
failure reason. Consistent with a possibility this project's own
adversarial review raised and never resolved: the identical `fail_reason`
string is mechanical (hardcoded on the challenge's `fail_if`, see the
known-limits correction above) and does not distinguish "forgot the
sequence" from "remembered it but drove crookedly into an 18cm-deep
doorway" -- and this benchmark independently documents real heading
drift of 10-22 degrees, up to 154, from a single bad contact (T14). If
localization precision, not memory, is bridge's real bottleneck, a
task-stack fix was never going to move it, and this result is consistent
with that being the case -- not proof of it; nothing here traced an
individual episode's turn-by-turn reasoning to confirm which.

**`counter_within_reach`: real, if partial, evidence the fix worked for
what it targeted.** A full clean pass on this specific challenge does not
appear anywhere else in this project's data -- not in the original single
run, not in any live baseline episode. It requires goal 2
(`Said([...], negate=True)`, "never claimed it was out of reach") to
never trip across the whole episode, which is exactly the failure this
fix targeted. The other 5 episodes did not reproduce the ORIGINAL failure
signature (an explicit false unreachability claim after already
delivering) at the top level -- they instead ended via "agent finished
its plan" at 0/2, a different, unexamined failure shape. This report does
not claim the original contradiction is proven gone in those 5: the
per-turn transcripts were not individually checked for it, only the
episode-level `reason` field, and saying more than that would be exactly
the kind of unverified claim this project's own discipline exists to
catch.

**Honest bottom line:** the fix is real, mechanically verified
(`test_taskstack.py` + `test_decide_checkpoint.py`, 34 assertions as
of T20's additions), and adversarially reviewed three times over -- but a
memory/state-persistence bug being real and fixed does not mean it was
THE bottleneck on every challenge it was diagnosed from. It measurably
helped the challenge most directly diagnosed as a memory failure and did
not help the challenge whose failure signature turned out, on closer
review, to be genuinely ambiguous between memory and localization. That
is a more precise, more honest result than either "the fix worked" or
"the fix didn't work," and it is the one the data actually supports.

## T18 re-verification: does surfacing blocked-drive streaks change anything?

FINDINGS.md's T18 adds `blocked_streak` (a harness-verified count of
consecutive blocked `turn`/`forward` primitives, surfaced as a warning in
the shared observation once it reaches 2) after tracing real episodes
showed the robot repeating a near-identical blocked action several times
in a row with no adaptation. Same three challenges, same n=6, same
before/after discipline as T17's re-verification:

| | T17-only (prior round) | T17+T18 |
|---|---|---|
| `bridge_three` | 0/6, 5/6 "wrong door" | 0/6, 6/6 "wrong door" |
| `bridge_five` | 0/6, 4/6 "wrong door" | 0/6, 5/6 "wrong door" |
| `counter_within_reach` | 1/6 (one full clean pass), 5/6 "agent finished its plan" | 0/6, 6/6 "agent finished its plan" |

**Bridge: still no measurable change, and a fresh trace explains why.**
0/12 before, 0/12 after, same dominant failure reason. A newly-traced
`bridge_three` episode (instrumented to log `blocked_streak` on every
turn, not assumed) shows `blocked_streak` at **0 for all 12 turns of the
episode** -- zero blocked-drive events happened at all. The robot instead
spent 8 of its 12 total turns purely adjusting its heading back and forth
(-35, -30, +60, -45, +15, -35, +65, -30 degrees -- real oscillation, not
noise) before ever attempting to drive forward, then went through the
wrong door on essentially its first real attempt. This is a DIFFERENT
failure mode than the one T18 was built from: not getting physically
stuck and failing to adapt, but a spatial/heading judgment that is simply
wrong, with no collision involved for the fix to have anything to react
to. Bridge's failures are not one thing -- some traced episodes show the
collision-loop T18 targets, this one shows pure misjudgment T18 has no
mechanism to address -- and the aggregate 0/12-to-0/12 is consistent with
a fix that works exactly as designed on the failure mode it targets while
that failure mode is not the only, or even the dominant, one in play.

**`counter_within_reach`: apparent regression (1/6 to 0/6), most likely
noise, disclosed rather than either hidden or oversold.** The single
clean pass from the prior round did not repeat. All 6 post-T18 episodes
failed via "agent finished its plan," 0/2 -- the same top-level reason 5
of 6 prior-round episodes already showed, just now 6 of 6. Going from 1
success in 6 trials to 0 in 6 is well within ordinary variance for a low,
noisy base rate on live, non-deterministic model calls; nothing in this
data distinguishes "T18 made this specific challenge worse" from "the
underlying success rate was always low and one lucky pass doesn't change
that." No repeat-of-repeats was run to settle it (that would need a much
larger n than this project's time budget supports), so it is reported as
an open, disclosed uncertainty, not resolved either direction.

**Honest bottom line, same standard as T17's:** the fix is mechanically
correct (independently verified by replaying real trace data against the
exact reset logic, not just trusting its own tests) and it measurably
changes the model's prompt exactly when the harness detects the pattern
it was built to catch. It did not move the aggregate pass rate on this
verification set, and a fresh trace shows a concrete, honest reason why
for bridge specifically: the mechanism the fix targets simply was not the
one that fired in that episode. A fix can be correct, real, and simply
not be aimed at the dominant cause of a given challenge's failures --
that is not a contradiction, and this project's discipline is to say so
plainly rather than credit a mechanically-sound fix with results it did
not produce.
