# GPT-6 cabinet skill

Skill: `innate-os/open_cabinet_with_gpt`, alongside
`innate-os/open_door_with_vision` on `codex/pull-held-handle`
(PR #737, includes the kitchen fixture).

## Source and adaptation

Jay Chooi's [tweet](https://x.com/chooi_jeq/status/2096064315115839904)
reports GPT-6 Astra robot control results. The closest verified public
implementation is Robocurve's
[inspect-robots-agent](https://github.com/robocurve/inspect-robots/tree/main/plugins/inspect-robots-agent).
Reference inspected at commit `7e4d1b7aee1c0d3cfc3a05a7492b9d12cda666f9`.
This is an independent, smaller adaptation of its observation → single bounded
motion → measured observation loop, two-image-observation history, and motion
notes. It is not a reproduction of the tweet's evaluation or success rate.
We reuse Innate's skill lifecycle, cameras, level IK and measured motion checks
instead of introducing a second robotics runtime or executing generated code.

The policy gets both cameras, measured wrist pose, named joint state/efforts,
and base odometry. It has absolute level-wrist targets, short base translations
and rotations, gripper commands, observe, done and give_up. No door-state/set-angle
or scene manipulation shortcut is exposed. Success is explicitly model-reported
visual evidence, not an independent physics score.

## Key and model

Set `OPENAI_API_KEY` in the **skills-server process environment**. For Docker,
add it to that simulator's private environment configuration and recreate the
container when ready; exporting it in an unrelated host terminal is insufficient.
Do not put a real key in this document or git. The skill fails before movement
if the key is missing. No Innate proxy or Gemini key is needed for this loop.

Default model: `gpt-6-astra`, Responses API, `service_tier: "priority"`, low reasoning,
4096 output-token cap.
Optional environment override: `INNATE_CABINET_MODEL` (exact API model ID).
See [official model documentation](https://developers.openai.com/api/docs/models/gpt-6-astra).
Actual model access must be checked with the supplied account key.

Start the house simulator on this branch, face the lower cabinet within about
60 cm, and ensure the arm path is clear and the gripper empty. Invoke
`open_cabinet_with_gpt` through the normal skill launcher (`max_steps=60`).
It stages a horizontal wrist at base XYZ (0.30, 0, 0.30) m. Size priors match the
fixture: a 12 cm vertical dark-metal handle centered 30 cm above the floor.

Wrist actions are capped at 3 cm and level-IK checked, including a conservative
−0.25 rad shoulder floor so the simulator clearance guard does not alter them; base steps at 3 cm,
turns at 0.12 rad, cumulative travel at 1 m, cumulative turning at 1.2 rad.
Fresh frames are required after each action. Three consecutive invalid motion
plans stop the run. Stop interrupts the API waiter; late responses cannot move
the robot. Cleanup halts arm/base and preserves the grip. No automatic release
or folding on failure. Skill debug traces store observations, notes and frames.

## Validation boundary

Contract tests exercise request history, bounded actions, malformed/incomplete
model output and cancellation while HTTP is in flight. Native ROS skill tests
exercise registration, camera/leveling flow, gripper/turn rejection, cleanup,
missing-key preflight and unreachable targets with mocked actuators/model.
These are scaffolding tests, not evidence of successful physical manipulation.
A complete GPT-6 grasp/pull run and independent hinge-angle verification remain
pending. Use the simulator for that full manipulation test.

Verified locally: 17 contract/native tests plus 46 existing door tests passed
in the ROS container (63 total); Ruff and diff checks passed.

Live simulator regression: staging reached (0.3014, 0.0005, 0.2975) m with
0.64 degrees pitch, then a real GPT-6 camera request selected a 2.5 cm left
move that tracked within 1.5 mm per axis. The run intentionally used
`max_steps=1` and ended with decision-budget exhaustion. The earlier staging
pose requested shoulder -0.83 rad, which the simulator limited to -0.25;
IK now rejects that pose before sending it. Horizontal checks remain intact.

The opening guidance now follows `open_door_with_vision`'s approximate 40 cm
outward pull, arm first and then base, before following the leftward hinge arc.
It keeps the existing 3 cm per-action limit and observations between moves.
A short tug without a visible gap is not an exhausted opening attempt, and
joint6's saturated closing effort is distinguished from excessive arm load.
Lost grasp, failed tracking, excessive arm effort, and visible obstructions
remain reasons to stop. No minimum pull is mechanically forced.

A model-only replay of the recorded premature-release observation (step 11,
run `b7018005768d4c7c9fc6bcb8c628b15b`), with historical telemetry/action attempts,
selected another 3 cm backward base step instead of releasing. No motion was
executed during that replay; full opening with this guidance is not yet verified.

## Equivalent Gemini skill

`innate-os/open_cabinet_with_gemini` runs the exact same `execute`, observation,
action, bounds, staging, cancellation and cleanup implementation as the GPT
skill. It uses the same `CabinetPolicy` prompt, tool schema, validation, full
text/state/action history and latest two pairs of camera images. Both expose
`max_steps=60`. The older `open_door_with_vision` remains a separate algorithm.

Only the policy factory changes: Gemini defaults to `gemini-3.8-flash` with
low reasoning via the existing Innate proxy (`INNATE_SERVICE_KEY` with Gemini
access), instead of GPT-6 via `OPENAI_API_KEY` and priority processing.
`INNATE_CABINET_GEMINI_MODEL` overrides its model ID independently of GPT.
The adapter translates the shared request into Gemini's OpenAI-compatible chat
format and preserves its full assistant message, including tool-call thought
signatures, across turns. OpenAI's service-tier setting is not sent to Gemini.

Validation: 72 tests passed across the two agent skills, adapter and original
door skill. The runtime loaded both skill IDs. Two real Gemini calls using
recorded cabinet cameras/telemetry returned valid actions in 2.60 s and 1.42 s;
the second included the first call's tool result. These were read-only API
checks; no Gemini-controlled full grasp/opening is claimed.
