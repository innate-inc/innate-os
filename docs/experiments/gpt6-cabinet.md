# GPT-6 cabinet skill

`innate-os/open_cabinet_with_gpt` opens the lower kitchen cabinet using
GPT-6 Astra, head/wrist cameras and measured robot state. PR #737 contains this
single cabinet-opening implementation and the hinged house-simulator fixture.

## Credentials

Managed robots use their existing `INNATE_SERVICE_KEY`. `CabinetPolicy` routes
OpenAI Responses requests through `innate_proxy.ProxyClient`; the provider key
stays in Innate's service proxy. No personal OpenAI key is required on the robot.

For standalone development without service credentials, set `OPENAI_API_KEY`
in the skills-server environment (the private root `.env` for the simulator).
It is used only when Innate service credentials are absent. A proxy access or
quota error fails the run instead of silently charging a personal account.
Missing both credentials fails before motion. Never commit credentials.

Default model: `gpt-6-astra`, `service_tier: "priority"`, low reasoning and
4096 maximum output tokens. `INNATE_CABINET_MODEL` can override the exact model
ID. Service credentials need access to OpenAI through the proxy.

## Running and inspecting

Start the house simulator, face the lower cabinet within about 60 cm, and
ensure the arm path is clear and the gripper empty. Launch
`open_cabinet_with_gpt` through the normal skill launcher. Its only input is
`max_steps` (default 60, allowed 1–100).

The skill stages a horizontal wrist at base XYZ (0.30, 0, 0.30) m. Each decision
gets labeled head/wrist images, measured wrist XYZ/orientation, joint
positions/efforts, odometry and the commanded grip state. It preserves text,
action results and reasoning state, plus the newest two image pairs.
The model returns one validated action with values and an evidence note.

Actions are absolute level-wrist targets, base translations/rotations, gripper
commands, observe, done and give_up. Wrist/base steps are capped at 3 cm, turns
at 0.12 rad, cumulative base travel at 1 m and cumulative turning at 1.2 rad.
Level IK includes a conservative −0.25 rad shoulder floor to respect the
simulator's base-clearance guard. Measured arm/base tracking is checked after
motion; fresh camera and arm-effort feedback is required. Turning while
commanded to grip is rejected.

Opening guidance calls for roughly 40 cm of outward progress, using the arm
then base in small steps before following the leftward hinge arc. This is
model guidance, not a mechanically forced minimum. Gripper closing effort is
separate from the arm load limit. Lost grasp, obstruction, excessive arm load
or failed tracking remain reasons to stop.

Stop interrupts the API waiter; a late response cannot move the robot. Cleanup
halts arm/base while preserving the grip. Three consecutive rejected plans,
API failure or decision-budget exhaustion fail the skill. Success is the
model's visual assessment after release, not an independent physics score.

Opt-in skill traces include camera frames, measured observations, decisions,
credential backend (never the key), tracking checks and terminal status.
Export the latest run with:

```sh
innate skill debug-export open_cabinet_with_gpt
```

## Source and validation

Inspired by Jay Chooi's [tweet](https://x.com/chooi_jeq/status/2096064315115839904)
and Robocurve's public
[inspect-robots-agent](https://github.com/robocurve/inspect-robots/tree/main/plugins/inspect-robots-agent),
inspected at commit `7e4d1b7aee1c0d3cfc3a05a7492b9d12cda666f9`.
This adaptation uses Innate's native skills, motion primitives and lifecycle;
it does not execute generated Python or expose privileged cabinet state.

Focused tests cover credential routing, proxy failure without direct fallback,
Responses history, action bounds, in-flight cancellation, native skill cleanup,
stale/excessive effort, invalid tracking, level IK and debug export.
Two live GPT-6 vision/tool requests through the proxy passed with
`OPENAI_API_KEY` unset, including multi-turn reasoning/tool-result history.
Those API checks used recorded camera observations and executed no motion.

Earlier simulator validation reached staging within 2.6 mm per axis at
0.64 degrees pitch, then executed a GPT-selected 2.5 cm move within 1.5 mm per
axis. Cleanup does not constitute a fresh end-to-end grasp/opening test or
physical-robot validation.
