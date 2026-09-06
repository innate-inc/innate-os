# Pickup speed experiment

Status: baseline preparation; no speed or reliability claim yet.

## Acceptance fixed before implementation

Time from submission of the ordinary `innate-os/pick_any_object` ROS action
through successful action completion and two seconds of stable post-carry hold.
An early grip followed by a carry drop is a failure, even if the skill reports
success. Simulator object state is an evaluator input only; the skill/model sees
robot cameras, odometry and joint telemetry. Report action latency and stable-hold
latency separately, along with every failed or timed-out trial.

The candidate must have median end-to-end latency at most 50% of baseline, with
no observed meaningful reliability regression on matched repeated trials. Use
three repeats per representative scenario at minimum: onboarding LEGO, rotated
LEGO, and a second graspable object. A baseline without repeatable stable holds
must first be made working; do not manufacture a percentage from all failures.
Freeze the scenario set before the comparison and retain slower cases. Apply a
180-second action timeout identically. Report sample size and uncertainty rather
than extrapolating a small simulator set to hardware.

Baseline source: the three pickup commits from draft #757 at `0cad4eb5`, reused
above draft #772 at `cc039cc4` (Astra transport). Local combined baseline is
`ee2d71a10`. Any reliability correction needed to establish a working baseline
will be identified and applied equally before timing candidate optimizations.

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
