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
Model overrides must support Responses vision/function calls, low reasoning,
and explicit prompt-cache breakpoints (as supported by GPT-6 Astra).

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

Model-facing telemetry is rounded to four decimal places and serialized as
compact JSON. Safety checks, motion control and recorded measurements retain
full precision. The text/action/reasoning history is append-only; the latest
two camera pairs follow it, labeled with their observation step. This lets
image replacement leave the reusable history prefix intact.

Requests use explicit caching with a 30-minute TTL, a stable key per skill run,
and breakpoints on the newest two telemetry messages. The previous breakpoint
remains available for lookup while the new one extends the cached prefix.
Camera frames follow the breakpoints, avoiding cache writes for transient
images. A short initial prefix may be below the cache minimum; hits are not
guaranteed on every request. See OpenAI's
[prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching).

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
Each returned API usage record is saved as `gpt_usage`, including step, actual
model/service tier, input tokens, cache reads/writes, output and reasoning
tokens. This includes incomplete/rejected model responses when usage is
available; a cancelled request may finish after the skill stops and have no
local usage record.
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
Responses history/cache prefixes, prompt-only rounding, usage records, action
bounds, in-flight cancellation, native skill cleanup,
stale/excessive effort, invalid tracking, level IK and debug export.
Two live GPT-6 vision/tool requests through the proxy passed with
`OPENAI_API_KEY` unset, including multi-turn reasoning/tool-result history.
Those API checks used recorded camera observations and executed no motion.

On September 7, 2026, replaying the same six saved observations through the
proxy with fresh model decisions gave the following usage. No robot interfaces
were loaded or motion executed. The new policy first wrote an eligible prefix
on decision 2, then reported cache hits on decisions 3–6, including after image
replacement. The sixth request read 2,140 cached input tokens.

| Six-decision total | Previous policy | Explicit caching + rounding |
| --- | ---: | ---: |
| Input tokens | 21,828 | 19,090 |
| Cache-read tokens (included in input) | 0 | 6,748 |
| Cache-write tokens (included in input) | 21,024 | 2,448 |
| Output tokens | 504 | 636 |
| Estimated API cost | $0.5921 | $0.3362 |

The estimate uses published
[Astra Fast rates](https://developers.openai.com/api/docs/models/gpt-6-astra):
$20/M ordinary input, $2/M cache reads, $25/M cache writes and $100/M output.
Input categories are disjoint; output already includes reasoning tokens.
This replay cost about 43% less. It is not a full-run cost forecast or a
closed-loop opening evaluation. All 31 focused cabinet tests and both debug
export tests passed in the ROS runtime after this change, including native
skill logging, cancellation and exported usage records.

Earlier simulator validation reached staging within 2.6 mm per axis at
0.64 degrees pitch, then executed a GPT-selected 2.5 cm move within 1.5 mm per
axis. Cleanup does not constitute a fresh end-to-end grasp/opening test or
physical-robot validation.
