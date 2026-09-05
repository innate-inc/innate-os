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

Default model: `gpt-6-astra`, Responses API, low reasoning, 4096 output-token cap.
Optional environment override: `INNATE_CABINET_MODEL` (exact API model ID).
See [official model documentation](https://developers.openai.com/api/docs/models/gpt-6-astra).
Actual model access must be checked with the supplied account key.

Start the house simulator on this branch, face the lower cabinet within about
60 cm, and ensure the arm path is clear and the gripper empty. Invoke
`open_cabinet_with_gpt` through the normal skill launcher (`max_steps=60`).
It stages a horizontal wrist at base XYZ (0.24, 0, 0.25) m. Size priors match the
fixture: a 12 cm vertical dark-metal handle centered 30 cm above the floor.

Wrist actions are capped at 3 cm and level-IK checked; base steps at 3 cm,
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
A real GPT-6 camera/grasp/pull run and independent hinge-angle verification remain
pending the key. Use the simulator for that first end-to-end run.

Verified locally: 16 new contract/native tests plus 46 existing door tests passed
in the ROS container (62 total); Ruff and diff checks passed.
