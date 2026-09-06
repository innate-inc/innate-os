# Pickup speed experiment

Status: the complete matched-v4 campaign at `dbe4de1ad` passes the fixed speed
and reliability gates: **53.313% lower median latency, 48.795 s to 22.781 s**,
with 9/9 durable pickups for each controller. This is a simulator result across
three repeats of three scenes, not a hardware or population-reliability claim.
Three predeclared original-speed comparison videos are complete and visually
checked. Final pickup-to-approach-to-throw compatibility also passed with the
separate shared cancellation fix in draft #781, described below.

## Final matched result

| Scenario | Classic median | Astra median | Durable successes, each |
|---|---:|---:|---:|
| Onboarding LEGO | 69.390 s | 23.325 s | 3/3 |
| Rotated LEGO | 45.293 s | 21.975 s | 3/3 |
| Cube | 47.632 s | 24.557 s | 3/3 |
| All nine attempts per controller | 48.795 s | 22.781 s | 9/9 |

Both successful-only and fixed-penalty all-attempt medians meet the predeclared
50% gate. The gate is aggregate; cube's scenario median improves by 48.44% and
does not individually halve. Every attempt passed the separate 20-second
post-action retention check. A long first-provider response in the first classic
rotated trial is retained in its 130.678 s result; later classic repeats took
45.182 and 45.293 s. No trial was discarded or replaced.

The [complete report](pickup-astra-speed/matched-v4-report.json) contains all
18 attempts, source hashes, medians and fixed gates. The
[reset audit](pickup-astra-speed/matched-v4-reset-source-audit.json) verifies
identical recorded sources, reset-state spread below 1e-5 and the
[predeclared plan](pickup-astra-speed/matched-v4-plan.json) fixes all scenarios,
controller order and repeat 2 for the three original-speed comparison videos.
Source hashes are reconstructed from frozen Git blobs, including the timing
instrumentation, and every trial's physics rate passed the 0.98–1.02 range.

The final candidate uses Astra/low for head localization, material and clearance,
then a referenced wrist image with a box and image-space object axis. Code
converts that axis to the bounded grasp angle. It overlaps head perception with
the navigation fold, uses fresh feedback to shorten settling, and avoids a
redundant head confirmation after three fresh optical-flow arrival frames while
still requiring wrist recognition. Lower search moves preserve every joint's
original travel bound at the same duration; a verified raised rigid grip avoids
the final carry fold. Closing force, descent increments, final centering and
committed closing/lifting durations remain unchanged. The shared rigid
floor-close correction and truthful carry result apply to classic too.

Validation on this production revision: 113 root tests, lint and diff checks;
actual absent-target failure without a grasp; actual Stop during moving wrist
alignment with loop exit 0.514 s after cancellation and no grasp started. Earlier
inference cancellation verifies that a late worker response cannot dispatch
motion. Independent read-only review found no actionable defects in the final
delta. No physical robot was used.

The final comparison used the coordinator-approved 290-call cumulative ceiling
and unchanged $5 review/stop bound. All provider usage is retained; estimated
cumulative list-price cost through this comparison is $2.9138445 across 275
calls. The final campaign itself used 27 classic calls ($0.123048) and 18 Astra
calls ($0.249300). Development
pilots and earlier failed campaigns below remain separate from this result.

## Final compatibility

The final integration at `a0c4bb7d2` used the three exact frozen pickup Git blobs
plus the separate shared cancellation fix in draft #781. Pickup succeeded in
20.623 seconds tool-to-result with two Astra calls. The LEGO stayed held for
79.304 seconds until deliberate throwing: 5,293 physics samples keep its center
at 0.14413–0.14654m above the floor. A later approach command succeeded (0.4m
requested, 0.261m observed between tool start and completion), then the throw
settled the LEGO inside the box and the current mission's physical judge passed.
The [compatibility summary](pickup-astra-speed/final-compatibility-summary.json)
records source identity, raw usage, physical measurements and the final result.
This compatibility timing is separate from the matched benchmark.

The first final compatibility attempt is retained as a failure: pickup succeeded
in 21.27 seconds and held for over 100 seconds, but navigation stalled after a
long suspension and the agent issued a new throw after a newer keep-holding
instruction. #781 owns that sequence-cancellation fix, a separate live Stop
check and 91 Gemini/native Responses tests. The successful pickup replay waited
while holding; it did not itself activate `stop_current_skill`. No frozen pickup
code was changed to repair that agent issue. Total pickup provider accounting,
including both compatibility attempts, is 279 calls with complete usage and an
estimated list-price cost of $2.9720095. Agent-loop calls belong to the separate
onboarding work and are not included in this pickup ledger.

## Acceptance fixed before implementation

Time from submission of the ordinary `innate-os/pick_any_object` ROS action
through successful action completion and two seconds of stable post-carry hold.
An early grip followed by a carry drop is a failure, even if the skill reports
success. Simulator object state is an evaluator input only; the skill/model sees
robot cameras, odometry and joint telemetry. Report action latency and stable-hold
latency separately, along with every failed or timed-out trial.

Before the final matched campaign, a sibling pickup-to-throw integration exposed
a slow slip several seconds after initial success. Add the same 20-second
post-action retention gate to both controllers: a late drop fails the trial.
Keep the fixed request-to-completion-plus-two-second latency measurement separate
from this durability validation; report both. Three-second pilot recordings do
not establish the longer retention gate.

The candidate must have median end-to-end latency at most 50% of baseline, with
no observed meaningful reliability regression on matched repeated trials. Use
three repeats per representative scenario at minimum: onboarding LEGO, rotated
LEGO, and a second graspable object. A baseline without repeatable stable holds
must first be made working; do not manufacture a percentage from all failures.
Freeze the scenario set before the comparison and retain slower cases. Apply a
180-second action timeout identically. Report sample size and uncertainty rather
than extrapolating a small simulator set to hardware.

Freeze scoring before the final matched set: a failed, rejected or timed-out
pickup contributes the same 180-second failure penalty. Report both this
all-attempt median and successful-trial latency, plus per-scenario outcomes.
Require the half-time condition on both medians, so failed baselines cannot
manufacture an apparent improvement. A working baseline needs stable holds in
at least two of three repeats of every scenario. Keep development pilots
separate from final frozen-code trials, retaining their failures and costs.

Baseline source: the three pickup commits from draft #757 at `0cad4eb5`, reused
above draft #772 at `cc039cc4` (Astra transport). Local combined baseline is
`ee2d71a10`. The recorded pilots additionally include the truthful post-carry
result check at `870b5107f`, applied to both controllers. Any other reliability
correction needed to establish a working baseline will be identified and
applied equally before timing candidate optimizations.

The final runner explicitly sets the native simulator's canonical `ARM_HOME`
targets before reset. Code inspection found that `reset()` otherwise preserves
the previous run's servo targets. The development pilots reset the scene/object
but did not normalize that arm state; their preliminary timing is not a matched
final comparison.

## Workflow and risks

Current path: fold navigation arm, settle head, Gemini 3.5 Flash localization,
optical-flow/odometry approach with Gemini reacquisition, open claw, wrist search
pose, Gemini wrist seed, CamShift correction and centimetre descent steps,
orient gripper, trajectory to floor, unpress/close, encoder check, bounded retry,
lift, encoder/FK or camera verification, repeated carry fold, action result.

Likely costs to measure include blocking motion/action completion, repeated
short wrist trajectories, reacquisition latency and redundant carry commands.
Astra's stronger perception/control may remove loops; a model swap by itself
does not establish faster pickup. Keep current joint, base velocity, reach,
collision, grip and committed-action constraints. Never loosen them to hit the
timing target. Preserve cancellation checkpoints, resource ownership, and fresh
observations following movement. A model worker may return data only, never
dispatch motion after cancellation.

## Verification

- Instrument phases and model usage without changing the baseline behavior.
- Run a small pilot to establish stable baseline and expose bottlenecks.
- Test bounded Astra control with strict, locally validated actions; record all
  provider calls, model/tier, latency, raw usage and estimated cost.
- Exercise ordinary caller, absent object, malformed/out-of-bounds action,
  cancellation during inference/movement and empty/slipped grasp with focused
  tests; run actual ROS/simulator trials and inspect recordings.
- Alternate baseline/candidate with identical reset states, physics, image
  resolution and speed settings; record concurrent compute load. Coordinate a
  quiet timing window rather than comparing different load conditions.
- Preserve unaltered-speed video and timestamps, exact revisions, scenario and
  outcome in an external visual evidence manifest. Keep PR draft.

## Local resources and experiment bounds

Exclusive ports 30600–30639, approved by coordinator. No hardware motion,
deployment or new cloud infrastructure. Initial pilot envelope: at most six
pickup attempts and 40 provider calls; stop on missing cost accounting or
provider failure. Candidate requests use Astra/low, default service tier,
`store=false`, and bounded output. Reassess measured pilot cost and value before
expanding to the matched set.

Official API references checked September 5, 2026:
[Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) and
[structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

## Pilot observations

Two original-controller onboarding LEGO trials completed in 97.16 and 97.99
seconds. Both needed three closes and retained the object after the carry fold.
Wrist alignment consumed 49–51 seconds across the attempts; five Gemini 3.5
Flash calls consumed 12.55–16.07 seconds per trial. Native physics ran at 1.000
times wall-clock speed in every recorded pilot. This differs from two sibling
onboarding observations in which the carry fold lost the object; preserve the
post-carry check and judge real held state rather than assuming success.

The first judge incorrectly required the object center to rise by the arm's
7cm clearance criterion. Camera review showed a stable held brick at 7cm above
the floor, versus its 1cm starting center. The judge was calibrated before any
candidate comparison to require at least 3cm center rise, nonempty claw, and
two seconds of position stability within 2cm in this isolated floor-object
fixture. The original erroneous judgement is preserved beside the correction.

Three initial Astra pilots failed: a search-height guard assumed the wrong
starting height; a lateral correction rejected normal 2mm settling below the
commanded stop; and a correctly centered grasp slipped on lift, then exhausted
its ten-call development guard during reacquisition. These are failures, not
fast pickups. The next prototype allows a model-selected close without the
blanket 1cm pre-close lift, retains the original floor/force limits, preserves
the raised rigid grip, and budgets enough bounded decisions for the existing
retries. The sixth pilot completed one close and held the brick at 14.5cm above
the floor: 47.199 seconds action latency, 49.199 including the initial stability
check, with a physics/wall ratio of 1.0002. Both camera views were inspected.
This is one successful development run, not the requested repeated proof.

Gemini pilot costs use all returned output tokens including the difference
between `total_tokens` and `prompt_tokens`, because proxy `completion_tokens`
omits thinking. The first ten calls cost an estimated $0.04336 at
[$1.50/M input and $9/M output](https://ai.google.dev/gemini-api/docs/pricing?authuser=1).
These are provider list-price estimates, not invoices.

Across all six development pickups, all 36 provider calls have returned usage.
The corrected estimated total is $0.43432, including Astra's cache-write input
surcharge ($12.50/M versus $10/M uncached input, $1/M cached input, and $50/M
output). The earlier estimate omitted that surcharge. The external cost ledger
preserves raw usage and each trial's subtotal.

Reassessment: one successful half-time pilot and complete low-cost accounting
warrant the frozen matched set and focused negative checks. Bound that next
stage at 200 cumulative provider calls, with a $5 estimated-cost review threshold
that stops subsequent requests. Stop for missing usage or provider failure.
The budget and phase wrappers now live exclusively in an explicit benchmark
overlay under `scripts/experiments/pickup`; production pickup has no global
experiment counter or cap.

## First matched round and revised candidate

The first six normalized trials at `be8435589` did not meet the speed target.
Astra retained all three objects for20seconds, at51.651/49.649/64.117seconds
(onboarding LEGO/rotated LEGO/cube). Classic completed the first and third at
69.059/48.171seconds; its rotated LEGO slipped during carry. Successful-trial
medians were51.651versus58.615seconds, only12% faster; the fixed-penalty medians
showed25%. This incomplete campaign is retained, not presented as final proof.

The revised prototype replaces repeated model wrist moves with one bounded
Astra observation/grasp plan and the existing cancellable camera servo. It also
uses Astra for head localization, overlaps that first look with the unchanged
navigation fold, and joins the fold before base search or teardown. A final
fresh-camera centering check and failure on lost tracking precede closing.

A lower search pose avoids raising the gripper only to lower it again. Offline
URDF FK agrees with KDL: NAV EE z0.065m, original search z0.198m, proposed search
z0.100m. From NAV, every joint travels no farther in the same two seconds; the
quintic trajectory's computed peak EE speed falls from0.3043to0.2675m/s, with
the same minimum EE z0.065m. Runtime falls back to the original search whenever
any joint would travel farther. This is a kinematic check, not collision or
pickup proof; real simulator validation remains required before comparison.

## Complete second campaign and shared reliability correction

All 18 matched trials at `34fbb11ab` are retained. Astra held 9/9 objects for
20 seconds. Classic held 6/9: onboarding LEGO 1/3, rotated LEGO 2/3, cube 3/3.
Successful-trial medians were 27.921 versus 57.869 seconds (51.75% lower), and
fixed-penalty medians 27.921 versus 68.677 seconds (59.34% lower). Despite those
ratios, the onboarding baseline fails the required 2/3 success gate. This is
not proof of the requested improvement against a working baseline.

All recorded source hashes match their frozen Git sources. Starting arm,
base, and per-scenario object state agree within 1e-5. The report now reconstructs
the expected files and temporary overlay from the frozen revision, rejecting
missing or changed working-tree sources even if every trial claims one HEAD.
Future recordings also include the benchmark runner and scoring scripts.

Real ROS negative checks acknowledged Stop during inference and ended without
starting a grasp; the late model response did not dispatch motion. An absent
target failed after the normal three-view scan without a grasp attempt. The
server represents cancellation with `success_type="cancelled"` while finalizing
the ROS goal successfully, so the recorder retains that field and the judge
explicitly rejects cancelled attempts.

Before another campaign, isolate one shared correction: known rigid material
closes at the existing floor limit without the 1cm pre-close lift, in both
controllers. Soft/unknown material keeps its previous handling. Every first
classic onboarding close in the second campaign caught air after that lift;
several later weak grasps slipped during lift or carry. No search, carry,
perception, force, floor, or motion-duration change belongs to this correction.
First run one classic pilot in each unchanged scenario and inspect first-close
aperture, lift, existing carry motion, and 20-second retention. Only a verified
correction warrants another frozen matched set with the same acceptance gates.
The estimated next stage is approximately 60 calls / $0.80, within the existing
200-call / $5 cumulative review bounds.

The shared correction at `a53d39dbc` passed all three classic pilots, including
20-second retention: 68.109 / 44.611 / 47.034 seconds. The onboarding first miss
remains, but the successful retry kept a wider aperture through lift and carry;
the rotated LEGO succeeded on its first close. This supports the narrow shared
correction, not a claim of population reliability.

The next Astra candidate combines claw opening with the existing two-second
search move, preserving verified-open recovery for a rejected move, shut claw,
or missing telemetry. It aims the search from the URDF shoulder origin. The
head plan distinguishes flat thin rigid targets (7cm search clearance), other
low rigid targets (10cm), and tall/soft/uncertain targets (original high search).
Every joint must still travel no farther than the original move from measured
state, at the same duration. From NAV, independent URDF FK gives a minimum EE
height of 6.500cm and peak quintic EE speed of 0.2302m/s for the flat pose,
versus 0.3043m/s originally. After the search has settled, the wrist observation
waits for a new camera frame instead of another fixed pause; frozen video fails
without inference or grasp. These are candidate optimizations, separate from
the shared floor-close correction, and require real simulator pilots.

The next pilots exposed a cross-camera identity gap: a correctly localized red
cube appeared nearly white from wrist glare and the model returned no match.
The prop had not moved. Wrist requests now include the last accepted head image
and selected head box as an identity reference, with boxes explicitly restricted
to the current wrist image. This preserves target continuity under exposure
changes rather than blindly relaxing color matching. The bounded continuation
allows 240 cumulative provider calls, retaining the $5 cost review/stop bound
and mandatory complete usage accounting; the coordinator authorized this modest
extension before the next campaign.

## Shared baseline correction and final candidate pilots

The original matched-v2 baseline failed its per-scenario reliability gate. The
shared correction at `a53d39dbc` skips the 1 cm pre-close lift for known rigid
objects in both controllers, retaining the original floor and force limits.
Classic pilots then held the onboarding LEGO, rotated LEGO and cube for 20 seconds
at 68.109, 44.611 and 47.034 seconds end to end respectively. These are pilots,
not a substitute for a complete matched baseline.

Candidate changes through `3607fed2e` preserve the SDK's verified gripper-open
recovery while combining open with search, choose bounded lower search poses
from the model's clearance classification, and use the last accepted head image
as an identity reference in the wrist request. Wrist boxes always refer to the
current wrist image. This recovered a cube whose color was washed out by glare.
Fresh head encoder and stationary odometry samples spanning at least 150 ms,
followed by a new camera frame, can finish the head settling wait early. Missing,
stale, moving or invalid feedback retains the original 1.2 second wait. Closing
and lifting durations, preload and settling waits remain unchanged.

The three v5 development pilots all passed the 20 second hold gate: LEGO 24.742 s,
rotated LEGO 23.304 s and cube 26.500 s. The first LEGO pilot used the shorter prompt
`the red LEGO`; a further pilot uses the frozen scenario's exact
`the red LEGO brick` prompt before the matched-v3 campaign. All matched trials
use `scenarios.json` exactly and the same frozen source hashes for both paths.

`negative-v5-wrist-stop` confirms real joint motion between wrist localization
and Stop, then exits the wrist stage 0.431 seconds after the acknowledged cancel
request. No grasp starts; only the existing safe arm teardown follows. The
server returns ROS status 4 with `success=true` and `success_type=cancelled`;
the latter is authoritative, and the benchmark rejects this as a pickup success.
The earlier absent-target and inference-cancel recordings remain retained.


## Matched-v3 result and next bounded pilot

All 18 attempts at `25ab908b005c5e58b237aec429fd9d56c3e7c703` passed the initial
hold and additional 20-second durability gate. Both successful-only and
all-attempt medians were 46.766963 seconds for classic and 25.822354 seconds for
Astra: **44.785% lower**, below the required 50%. Per-scenario success counts
were 3/3 for both controllers on onboarding LEGO, rotated LEGO and cube. Exact
source hashes agree with the frozen revision; reset-state differences were
below 1e-5 and all physics-rate checks passed. Every attempt remains retained.
Cumulative usage through this campaign: 211 calls, complete usage records,
estimated $2.259 at the recorded list prices.

The next pilot adjusts nominal low-search reach from 0.30m to 0.315m, and flat
search reach to 0.306m, addressing repeated forward camera-servo corrections.
A proposed full 2cm shift exceeded the original joint-travel envelope and was
rejected. The chosen poses keep every joint's travel at or below the original,
with the same 2-second motion; independent URDF checks give peak EE speeds
0.249m/s and 0.238m/s versus 0.304m/s originally. The runtime travel guard still
falls back to the original pose for unfavorable starting states.

Astra also permits three fresh optical-flow arrival frames to hand off directly
to mandatory wrist-model target recognition. This only applies to a successful,
projectable `in_box` result; timeouts, invalid projections, classic callers and
blind grasps keep head confirmation. Neither the wrist recognition requirement
nor the final centering, force, descent, cancellation or hold gates is relaxed.
These are candidate changes awaiting real pilots and a new complete comparison,
not adjustments to the frozen matched-v3 result.


The v6 pilots retained all three objects for20seconds, but provider latency
left insufficient timing margin. The next candidate splits the same forced
Astra tool by view: head output contains box, material handling and clearance;
wrist output contains only box and roll, using the accepted head material.
The executor already owns rigid floor closing and soft unpressing, so the
redundant model closing-style choice is removed. The model, reasoning effort,
reference images, force/motion limits, fresh-frame checks and scorer stay fixed.

The smaller per-view request reduced token usage but did not remove the long
wrist inference in its LEGO pilot. The next pilot requests an image-space long
axis as two normalized points instead of a numeric roll. The executor rescales
horizontal/vertical coordinates to pixels, computes the minor-axis angle and
clamps it to the unchanged +/-1.5rad limit. Empty axes retain the unrolled grasp
for square/round objects. Degenerate or invalid axes are rejected. This retains
continuous grasp angles while moving their trigonometric conversion into code.
