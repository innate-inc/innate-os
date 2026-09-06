# Pickup speed experiment

Status: development pilots; no speed or reliability improvement claim yet.

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
