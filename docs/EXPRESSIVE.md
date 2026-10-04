# Expressive motion

MARS expresses emotion with its whole body: from a short prompt ("curious", "a cat spotting a
cucumber", "proud, you solved it") to lifelike motion, alive between gestures and swaying with its
own voice. The design ports Binh Pham's Reachy Mini harness (planner LLM writes a recipe, a
generator adds organic detail, an animator blends gestures, idle breathing and speech) onto MARS
through a semantic pose basis (PR #679's idea).

```
prompt ─ planner (LLM) ─► recipe ─ dsl.expand ─► frames ─ plan.to_plan ─► plan (keys every 0.25 s)
      ─ liveliness (or a learned generator) ─► motion (8 channels, 25 Hz)
      ─ basis.synthesize ─► actuator poses ─ animator (idle · crossfades · speech sway) ─► robot
```

## The body as an instrument

MARS's degrees of freedom are functional (a 5-joint arm and gripper, a head that only tilts, a
differential base), so the planner never sees joints. It writes in eight body-language channels
(`brain_client/expressive/channels.py`, order frozen):

| ch | DSL | range | the body |
|---|---|---|---|
| approach | `a` | -1..1 | arm and head pull back into the body ↔ lean in toward the person |
| expand | `x` | -1..1 | fold small, gripper shut ↔ arm out wide, gripper open |
| rise | `z` | -1..1 | arm folded down low ↔ raised like a mast |
| attend | `p` | -1..1 | head -20° (gaze down) ↔ +20° (gaze up) |
| askew | `k` | -1..1 | wrist roll + arm cant: the cocked-head quality |
| orient | `b` | ±60° | base turn from where expression started |
| advance | `d` | ±0.25 m | base forward / back from where expression started |
| grip | `g` | 0..1 | the gripper is the mouth: gasp, chatter, bite, yawn |
| energy | `E` | 0..12 | how much fast detail the generator adds (plan only) |

`basis.json` maps them to actuators. It is data, tuned by rendering it (`mars-express sheet`):

- **NEUTRAL is the home fold** (j1 1.40, j2 -1.15, j3 1.45, j4 0.50): the at-ease pose the arm can
  hold for hours (gravity torque ~0.17 N·m on the elbow, as at rest) and that the head camera
  cannot see (0 arm pixels in a segmentation render of the `main` camera).
- Each body channel has a -1 and a +1 endpoint, stored as actuator deltas. The negative ends of
  approach, expand and rise tighten and droop the fold and dip the head, so they are subtle; the
  positive ends **unfold** the arm: approach +1 reaches forward toward the person, rise +1 stands
  a mast beside the head, expand +1 opens the arm out to the robot's left with the gripper open.
- Unfolding is shared: those three positive ends are stored relative to READY (a raised front
  pose) and `offset = u·(READY − NEUTRAL) + Σ|w|·endpoint` with `u = 1 − Π(1 − w⁺)`. One channel
  alone interpolates linearly to its endpoint; tall + reaching becomes the mast leaning in instead
  of two unfoldings summed into a knot.
- The robot has no collision model, so the basis carries its own: a `safe` table (7⁵ nodes over
  the five body channels, multilinear) that scales the arm channels toward the fold where a
  combination would fold the arm into itself, the chassis or the floor. `mars-express basis`
  rebuilds it against a robot-only MuJoCo model of mars.urdf (`reach.py`: contacts confirmed with
  GJK distance, base_link–link2 excluded like arm.srdf): every −1/0/+1 combination of the body
  channels is clear, and 26 of 3000 random rows touch by at most 1.9 mm.
- Then joint limits and the shoulder-clearance rule (across the front arc j2 ≥ −0.25, the sim's
  ramp). `max_speed` (6 rad/s arm, 10 rad/s gripper, 200 °/s head) is enforced per tick by the
  animator, which also only lets j1 sweep into the front arc as fast as j2 can rise over its floor.

Head-camera occlusion at the endpoints (share of the image covered by the arm,
`out/axis_sheet_maincam.png`): expand +1 32 %, rise +1 25 %, attend −1 16 % (looking down at the
fold), askew −1 8 %, approach +1 6 %, approach −1 and rise −1 3 %, everything else 0.

## Modules (`ros2_ws/src/brain/brain_client/brain_client/expressive/`, pure Python + numpy)

| module | job |
|---|---|
| `channels` | channel order, ranges, NEUTRAL |
| `prng` | mulberry32: every random draw, bit-identical to the studio's JS port |
| `dsl` | recipe language (`go` / `hold` / `osc`), `expand`, `check`, `variants` |
| `plan` | sparse keyframe plans, scipy-exact zero-phase low-pass in numpy, `extract` from motion |
| `liveliness` | the procedural generator: time-warp jitter, an overshooting tracker, energy-scaled band-limited noise |
| `basis` (+ `basis.json`) | channels → `ActuatorPose`; safe table, clamps, per-tick speed limit |
| `reach` | robot-only MuJoCo collision check of mars.urdf (host only; builds the safe table) |
| `drive` | `BaseTracker`: orient/advance offsets → a differential-drive twist from odometry |
| `motion` | `Clip`: the JSON that plays in the browser, the sim and the robot |
| `animator`, `breathing`, `speech` | idle breathing, crossfaded play/queue/stop, speech sway, masks, gaze, `enter_from` the measured pose |
| `prompt`, `planner` | the frozen planner prompt; write → check → repair against any chat function |
| `presets` | built-in recipes (the never-silent fallback) |
| `probes` | physical probes: does the sneeze release go down, does "no" shake the base… |

## The procedural generator

`liveliness.animate(plan_frames, seed)` turns a plan into 25 Hz motion, deterministic for a seed
(every draw from one documented mulberry32 stream, so the studio's JS port matches to 1e-4):

1. timing jitter: the plan is read through a smooth time warp (±50 ms, pinned at both ends);
2. a second-order tracker per channel, critically damped on the error with the target's velocity
   fed forward: no lag on slow segments, a small overshoot (~2-4 % of the move) after fast ones;
3. band-limited noise (5 sinusoids per channel, log-uniform in a per-channel band) scaled by
   `E · NOISE_SCALE` and sped up with energy: E 1 is a calm drift, E 8 a tremble (head ~3° RMS,
   arm 0.02-0.1 rad RMS).

`plan.extract(motion)` inverts it for training data: the RMS of the detail above 1 Hz divided by
the same scale comes back as the plan's energy.

## Viewing it in the simulator (no robot, no ROS)

The host tools live in `expressive/` (a uv project; MuJoCo renders through `VirtualMars`, the
sim's own world model, `void` environment):

```bash
cd expressive
uv sync
uv run mars-express sheet        # out/axis_sheet_{front,3q,maincam}.png: every channel at -1 / 0 / +1
uv run mars-express presets      # out/presets/<name>.{mp4,png,json} for the 16 built-in presets
uv run mars-express plan "a cat spotting a cucumber" --chat gemini --out out/cat.json   # GEMINI_API_KEY
uv run mars-express plan "a cat spotting a cucumber"                                    # preset fallback
uv run mars-express render out/cat.json out/cat.mp4 [--camera three-quarter|front|profile|split]
uv run mars-express probe recipes.jsonl     # {"prompt", "recipe"} per line; --teacher scores probes.TEACHER
uv run mars-express basis                   # rebuild basis.json's safe table after editing endpoints
uv run mars-express golden                  # fixtures/golden.json, the cross-language port check
```

Cameras are fixed in the frame where the robot started (the person stands still, so advance and
orient read as the robot moving): `three-quarter` (the default: 1.6 m away, 25° down, 35° off the
robot's heading) for judging shapes, `front` for a person leaning in (eyes 1.3 m up, 1.4 m away),
`profile`, and `main` (the robot's own head camera, for occlusion). Clips play physically: arm and
head through the sim's servos (`set_joint_target`), the base by `drive.BaseTracker`, the same
odometry P-controller with feed-forward the robot uses. Contact sheets pose the robot kinematically.

## Running on the robot / in the sim

The expression layer runs inside the brain client node (`brain_client/expressive_driver/`, no
extra ROS node): one `Animator` ticked by a 30 Hz node timer, its poses mapped onto the hardware.

| output | topic | when |
|---|---|---|
| head | `/mars/head/set_position` (Int32, °) | when the rounded degree changes |
| arm | `/mars/arm/commands` (6 rad, the streaming pass-through) | every tick while a clip plays, the robot speaks, or an agent runs with idle breathing on; a stream starts from the measured joints and is slewed at ≤ 1.8 rad/s per joint |
| base | the brain's `cmd_vel_topic` (`/cmd_vel_skills` → the mux on hardware, `/cmd_vel` in the sim) | while a clip's orient/advance is non-zero or the robot is off its anchor: feed-forward P-control on `/odom` (`stance.py` over `expressive.drive`), ≤ 0.6 rad/s and 0.15 m/s, one zero twist on arrival, then silence |

A robot nobody talks to stays still: with the brain inactive and nothing playing, nothing is
published. The layer yields the body to whatever else owns it:

- a running skill (any `running` on `/brain/skill_status_update` except `express`, or the brain's
  own skill slot) or a live Nav2 `/navigate_to_pose` goal masks everything: output stops at once,
  the animator eases to neutral behind it, and the end of the skill plays a small reaction;
- a `/mars/arm/commands` or `/mars/head/set_position` command that is not ours (leader arm, UDP
  teleop, the arm SDK page, the head slider) holds that part off for 5 s after its last message;
  `/joystick` holds the base off for 2 s;
- the gaze tracker's tilt goes to `Animator.set_gaze` (the expression rides on it), and the base is
  left to the tracker's own panning;
- the 3 s rest fold on agent activation is not interrupted (the arm waits 3.5 s);
- a claw that holds something (commanded closed, measured open; hardware only, the sim does not
  report commanded joints) keeps its grip.

Speech sway: the TTS loop hands the robot's own voice (never the sim's simulated residents) to
`Animator.feed_speech` as it reaches the speaker: PCM s16le 16 kHz chunks on hardware, and in the
sim the whole 44.1 kHz WAV once, when it is published on `/tts/audio`.

### ROS surface

| topic | type | |
|---|---|---|
| `/brain/express/prompt` | String | a prompt, or `{"prompt", "id"}`: generate and play |
| `/brain/express/play` | String | a Clip JSON to play now |
| `/brain/express/stop` | Empty | back to idle |
| `/brain/express/state` | String, 5 Hz | `{playing, name, t, duration, idle, masked, speaking, source, id}` |
| `/brain/express/generate_req` → `/brain/express/generate_res` | String | `{id, prompt}` → `{id, clip, source}` or `{id, error}`, without playing |

`source` says which link of the chain made the clip: `server` (the planner server at
`expressive.server_url`, probed every 30 s off the prompt path, 1.5 s timeout), `llm` (the brain's
own model writing a recipe for the procedural generator, ~2 s with Gemini Flash at minimal
thinking), `preset` (the best keyword match), or `played`.

Parameters on brain_client_node:

| parameter | default | |
|---|---|---|
| `expressive.enabled` | `true` | `false` builds nothing; speech, gaze and the prompt behave as before |
| `expressive.idle_breathing` | `true` | breathe while an agent runs; `false` holds still between clips |
| `expressive.server_url` | `http://innate52.local:8000` | the planner server; `""` skips it |
| `expressive.on_skill_completed` | `pleased, small nod` | reaction prompt; `""` for none |
| `expressive.on_skill_failed` | `deflated` | |

The agent emotes through tags in its replies. The system prompt asks for
`<emote>2-8 words of body language</emote>` at the start of a reply and on emotional beats; the
speech streamer cuts them out (never spoken, never shown in the chat) and plays each one when its
sentence goes to TTS. Skills call `express(prompt, wait=True)` (`workspace/innate_skills/express.py`).

### Try it in the sim

```bash
./innate-sim up
innate skill run innate-os/express @prompt="proud, chest out"     # inside ./innate-sim sh
```

Over rosbridge (`ws://localhost:9090`), publish `/brain/express/prompt` (std_msgs/String) or a
Clip JSON on `/brain/express/play`. On the Agent page, start an agent and chat: replies open with
an emote, the head and wrist sway while the browser plays the voice, and the arm breathes between
turns.

### Hardware checklist

- [ ] speech sway against `aplay`: chunks are fed as they are written, so the sway may lead the
      voice by the ALSA buffer; set the Animator's `speech_latency_s` if it does
- [ ] the arm streaming at TELEOP gains for minutes while an agent runs: servo temperature, and
      whether the rest fold should be skipped when expression owns the idle arm
- [ ] the head servo at up to 30 commands/s during speech
- [ ] the stance through the mux: `/cmd_vel_skills` outranks Nav2, and is published only while
      the base is being corrected
- [ ] grip guard: pick an object, chat, the object stays held
- [ ] an agent with gaze on (inspireface is missing in the sim): tilt rides the expression, wheels
      pan with no stance fighting them
