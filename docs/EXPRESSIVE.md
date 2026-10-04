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
- Each body channel has a -1 and a +1 endpoint, stored as actuator deltas. The negative ends stay
  compact: approach −1 pulls the arm in tight with the head down 8°, rise −1 is the slump (claw
  turned down, head down 14°), expand −1 the closed fold. The positive ends **unfold** the arm:
  approach +1 reaches forward toward the person, rise +1 stands a mast beside the head, expand +1
  opens the arm out to the robot's left with the gripper open.
- Why compact, measured (basis v6, the blind eval below): every way of making the negative ends
  visible by moving the arm (a droop that reaches the floor, a recoil that raises the claw beside or
  in front of the head) was read by the blind judges as a gesture ("inspecting the floor",
  "raising its hand", "head scratch") and cost 6-12 points on the held-out prompts, where the
  planner writes small negative a/x/z all the time. A low, still arm with the head down reads
  "dejected" instead; the base (backing away, turning away) carries the rest. An "open" at-ease
  NEUTRAL (claw raised to the chest) read as "inspecting its claw" and was dropped for the same
  reason.
- Unfolding is shared: those three positive ends are stored relative to READY (a raised front
  pose) and `offset = u·(READY − NEUTRAL) + Σ|w|·endpoint` with `u = 1 − Π(1 − w⁺)`. One channel
  alone interpolates linearly to its endpoint; tall + reaching becomes the mast leaning in instead
  of two unfoldings summed into a knot.
- The robot has no collision model, so the basis carries its own: a `safe` table (7⁵ nodes over
  the five body channels, multilinear) that scales the arm channels toward the fold where a
  combination would fold the arm into itself, the chassis or the floor. `mars-express basis`
  rebuilds it against a robot-only MuJoCo model of mars.urdf (`reach.py`: contacts confirmed with
  GJK distance, base_link–link2 excluded like arm.srdf): every −1/0/+1 combination of the body
  channels is clear, and 19 of 3000 random rows touch by at most 4.9 mm.
- Then joint limits and the shoulder-clearance rule (across the front arc j2 ≥ −0.25, the sim's
  ramp). `max_speed` is the hardware's TELEOP speeds (j1 6.0, j2 3.6, j3 4.8, j4 2.4, j5 2.4,
  j6 1.4 rad/s; head 200 °/s). A clip is first **retimed** to them (`Basis.retime`: a move too
  fast for a joint is slowed down until it fits, judged on the motion low-passed at 4 Hz, so a
  snap keeps its full excursion instead of being cut short), then the animator rate-limits every
  tick as the safety net, letting j1 sweep into the front arc only as fast as j2 can rise over its
  floor.

Head-camera occlusion at the endpoints (share of the image covered by the arm,
`out/axis_sheet_maincam.png`): expand +1 32 %, rise +1 25 %, attend −1 15 % (looking down at the
fold), rise −1 10 %, approach +1 6 %, askew −1 6 %, approach −1 2 %, everything else 0.

Blind recognition (`mars-express eval --judge openai --n 3`, basis v6, see
`expressive/out/eval/ITERATIONS.md` for every iteration): the 16 hand-written presets are named
in the judge's top 3 labels 62 % of the time and described as the prompt or something related 75 %
of the time (38 % / 58 % before); the planner's recipes for 30 held-out prompts are described as
related 72 % of the time. The judge reads any raised arm as a raised hand or a wave and any still,
low clip as "inspecting", which is what still costs proud, scared, angry, affectionate and bored.

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
| `presets` | 17 built-in recipes, `listening` the default; `match` scores keyword stems (the never-silent fallback) |
| `probes` | physical probes: does the sneeze release go down, does "no" shake the base… |

## The procedural generator

`liveliness.animate(frames, seed)` turns conditioning frames into 25 Hz motion, deterministic for a
seed (every draw from one documented mulberry32 stream, so the studio's JS port matches to 1e-4).
The procedural path (`Clip.from_recipe`, the studio, the robot's fallback) animates the expanded
recipe itself: the sparse serving plan's 2 Hz low-pass kept only 9-61 % of an `osc` at 0.3-0.6 s
periods, which is what nods, bounces and chatter are made of (`osc 2 p .4 .4` now reaches 110 % of
its amplitude on the head). Learned generators keep the sparse plan they are trained on.

1. timing jitter: the plan is read through a smooth time warp (±50 ms, pinned at both ends);
2. a second-order tracker per channel, critically damped on the error with the target's velocity
   fed forward: no lag on slow segments, a small overshoot (~2-4 % of the move) after fast ones;
3. band-limited noise (5 sinusoids per channel, log-uniform in a per-channel band) scaled by
   `E · NOISE_SCALE` and sped up with energy: E 1 is a calm drift, E 8 a tremble (head ~3° RMS,
   arm 0.02-0.1 rad RMS). Orient and advance get no noise: the base moves only when the recipe says so.

`plan.extract(motion)` inverts it for training data: the RMS of the detail above 1 Hz divided by
the same scale comes back as the plan's energy.

## Viewing it in the simulator (no robot, no ROS)

The host tools live in `expressive/` (a uv project; MuJoCo renders through `VirtualMars`, the
sim's own world model, `void` environment):

```bash
cd expressive
uv sync
uv run mars-express sheet        # out/axis_sheet_{front,3q,maincam}.png: every channel at -1 / 0 / +1
uv run mars-express presets      # out/presets/<name>.{mp4,png,json} for the 17 built-in presets
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
| head | `/mars/head/set_position` (Int32, °) | when the rounded degree changes; a resume after another owner had the head blends from the measured tilt over 1.5 s |
| arm | `/mars/arm/commands` (6 rad, the streaming pass-through) | every tick while a clip plays, or an agent runs and speaks or breathes (idle breathing on); each time the stream (re)starts the animator enters from the measured pose over 1.5 s (`Animator.enter_from`), and it rate-limits every joint (`basis.json` `max_speed`) |
| base | the brain's `cmd_vel_topic` (`/cmd_vel_skills` → the mux on hardware, `/cmd_vel` in the sim) | only while a clip's orient/advance leaves the deadband (1.5°, 1 cm) or the robot is off its anchor: feed-forward P-control on `/odom` (`stance.py` over `expressive.drive`), ≤ 0.6 rad/s and 0.15 m/s, one zero twist on arrival, then silence. Gestures that never turn or step publish nothing |

A robot nobody talks to stays still: with the brain inactive and nothing playing, nothing is
published. Deactivating the agent, or `/brain/express/stop`, stops everything at once: the clip on
stage and any clip still being generated are dropped, the arm stream ends within the 0.4 s
crossfade, and a stance in progress gets one zero twist and stops where it is — no trip back to
its anchor. The layer yields the body to whatever else owns it:

- a running skill (a `running` on `/brain/skill_status_update`, or the brain's own skill slot) or
  a live Nav2 `/navigate_to_pose` goal masks everything: output stops at once, and the stream
  resumes from wherever the skill left the body. Masking is silence rather than
  `Animator.set_mask`, whose eased-to-NEUTRAL parts would start the resumed stream at NEUTRAL
  instead of the measured pose. Skills that never move the body leave expression running:
  `LEAVES_BODY` in `expressive_driver/utils.py` (`search_memory`, `change_volume`, and `express` itself). It is an
  allowlist because a skill's declared interfaces cannot prove it body-free —
  `navigate_to_position` and `navigate_with_vision` drive the base through raw ROS clients without
  declaring `Mobility` — so an unknown skill masks. Add a shipped skill there when it is body-free;
- when a body skill ends while an agent runs, a small reaction plays at once
  (`expressive.on_skill_completed` / `on_skill_failed`, preset names, so no LLM call), unless the
  agent's reply carried an emote in the last 3 s; a skill run by hand on an idle robot draws none;
- a `/mars/arm/commands` or `/mars/head/set_position` command that is not ours (leader arm, UDP
  teleop, the arm SDK page, the head slider) holds that part off for 5 s after its last message
  (each of our own commands is recognized once, by its echo); `/joystick` holds the base off for 2 s;
- Mad drive mode (read from `/robot/info`, 1 Hz) holds the arm off: mars_app folds it into a
  bracing pose for Mad's speeds, and a stream would undo the fold. The signal can lag the mode
  switch by up to a second, so a stream that is running at that moment may cut into the brace goto;
- the gaze tracker's tilt goes to `Animator.set_gaze` (the expression rides on it), and the base is
  left to the tracker's own panning;
- the 3 s rest fold on agent activation is not interrupted (the arm waits 3.5 s);
- a claw that holds something (commanded closed, measured open; hardware only, the sim does not
  report commanded joints) keeps its grip.

Speech sway (only while an agent runs, or on top of a playing clip): the TTS loop hands the
robot's own voice (never the sim's simulated residents) to
`Animator.feed_speech` as it reaches the speaker: PCM s16le 16 kHz chunks on hardware, and in the
sim the whole 44.1 kHz WAV once, when it is published on `/tts/audio`.

### ROS surface

| topic | type | |
|---|---|---|
| `/brain/express/prompt` | String | a prompt, or `{"prompt", "id"}`: generate and play |
| `/brain/express/play` | String | a Clip JSON object to play now (never a path; malformed clips are logged and ignored) |
| `/brain/express/stop` | String (payload ignored) | back to idle. Not `Empty`: rosbridge (rws) serializes `std_msgs/Empty` to 0 bytes, which every ROS subscriber rejects |
| `/brain/express/state` | String, 5 Hz | `{playing, name, t, duration, idle, masked, speaking, source, id}` |
| `/brain/express/generate_req` → `/brain/express/generate_res` | String | `{id, prompt}` → `{id, clip, source}` or `{id, error}`, without playing |

A prompt moves the robot at once: its keyword preset (`presets.match`) starts playing as a
stand-in (source `preset-stand-in`) while the real clip is generated, and the generated clip
crossfades in when it arrives. When the chain only reaches the preset, the stand-in is relabelled
`preset` (final) and plays out; a newer prompt, play or stop drops a clip still being generated.
Clips are synthesized to actuator frames on a worker thread, never on the ROS executor or under
the driver's lock, so emotes cost the speech stream and the 30 Hz tick nothing.

`source` says which link of the chain made the clip on stage: `server` (the planner server at
`expressive.server_url`, probed every 30 s off the prompt path, 1.5 s timeout), `llm` (the brain's
own model writing a recipe for the procedural generator, 2-7 s with Gemini Flash at minimal
thinking), `preset` (the best keyword match, or a preset reaction), `preset-stand-in`, or `played`.

Parameters on brain_client_node:

| parameter | default | |
|---|---|---|
| `expressive.enabled` | `true` | `false` builds nothing; speech, gaze and the prompt behave as before |
| `expressive.idle_breathing` | `true` | breathe while an agent runs; `false` holds still between clips |
| `expressive.stand_in` | `true` | play the keyword preset while a prompt's clip is generated |
| `expressive.server_url` | `""` | the planner server (`http://<host>:8000`); empty skips it. Set it per robot in `config/settings.yaml` (stanza below) |
| `expressive.on_skill_completed` | `agreeing` | a preset name plays instantly; other text is a prompt to generate; `""` for none |
| `expressive.on_skill_failed` | `sad` | |

The agent emotes through tags in its replies. The system prompt asks for
`<emote>a feeling plus one physical cue, 2-8 words</emote>` at the start of a reply and on
emotional beats, invented fresh each time (its examples span proud, sheepish, startled and subtle,
and it is told never to reuse one: with a single example the model copied it verbatim); the
speech streamer cuts them out (never spoken, never shown in the chat) and plays each one when its
sentence goes to TTS. Skills call `express(prompt, wait=True)` (`workspace/innate_skills/express.py`).

To point a robot (or the sim) at a planner server, add to the gitignored `config/settings.yaml`
and restart the brain:

```yaml
brain_client_node:
  ros__parameters:
    expressive:
      server_url: "http://192.168.0.156:8000"   # use the IP: .local names do not resolve inside the sim container
```

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
- [ ] `basis.json` `max_speed` (the per-joint TELEOP profile speeds: j1 6.0, j2 3.6, j3 4.8, j4 2.4,
      j5 2.4, j6 1.4 rad/s) against the pass-through's soft gains; the arm SDK's own stream cap is 1.8 rad/s
- [ ] the head servo at up to 30 commands/s during speech
- [ ] Mad mode while an agent emotes: the brace fold completes (the arm hold reads `/robot/info`,
      up to 1 s behind the mode switch)
- [ ] the stance through the mux: `/cmd_vel_skills` outranks Nav2, and is published only while
      the base is being corrected
- [ ] grip guard: pick an object, chat, the object stays held
- [ ] an agent with gaze on (inspireface is missing in the sim): tilt rides the expression, wheels
      pan with no stance fighting them

## Evaluation

`expressive/eval/` judges the motion blind, as Binh's "judge with videos" and PR #679's panel did:
a vision model watches a clip, is never told the prompt, and says what it sees. Full results, strips
of the best and worst reads, and the analysis: `expressive/eval/REPORT.md` (regenerated into
`expressive/out/eval/REPORT.md`).

```bash
cd expressive
uv run mars-express eval --judge gemini --n 3    # Gemini 3.1 Pro watches each video (via the Innate proxy)
uv run mars-express eval --judge openai --n 3    # GPT-5.5 reads each 2x4 key-frame strip
uv run mars-express eval --report-only --snapshot eval/   # rebuild REPORT.md + the committed copy
```

- **Prompts.** The 16 presets twice (the hand-written recipe and one the planner writes for the same
  prompt), and 30 held-out prompts no preset covers (embarrassed, disgusted, a snake rearing up, a
  grumpy neighbour, a cat hunting a mouse, tipsy, winning the lottery, crossing a street, the cookie
  jar…). The planner is `gpt-6-astra` through `planner.write`.
- **Arms.** Each recipe is played two ways: **lively** (the plan through `liveliness`, what ships) and
  **direct** (the same plan, no liveliness, the control). An arm is any function from the serving plan
  (`plan.frames(plan.to_plan(dsl.expand(recipe)))`, (T, 9) at 25 Hz) and a seed to (T, 8) motion. The
  ml workstream's flow generator joins with `uv run --extra flow mars-express eval --flow
  runs/generator/generator.pt --pair flow,lively` (it wraps `ml.generator.sample.Generator.generate_frames`;
  the checkpoint is rsynced from the 5090). Any other arm joins with `--arm name=module:function`.
- **Media.** A 2x4 key-frame strip (farthest-point sampled, so a 0.2 s snap makes the strip) and a
  caption-free mp4 played physically in the sim, both from the three-quarter view.
- **Judges.** Three independent calls per clip. Each spreads probability over the studio's 17 labels
  (read from `webapp/js/expression/judge.js`, so the two judges cannot drift), gives one free-text
  description, and rates alive and readable 1-5. A text grader then scores each description against
  the prompt (2 same, 1 related, 0 different). A pairwise judge picks the more alive of the `--pair`
  arms (lively and direct by default). Every round shows the pair in both orders, and a win counts only
  when both orders agree. A round where the same slot won both times counts as position-biased.
- **Planner.** First-pass validity, repairs and latency of every write, and the 18 physical probes ×
  8 samples.
- Everything is cached under `out/eval/`. A clip that changes (a new core, a retuned basis) drops its
  media and judgments, and the core commit is recorded with the run.

Results on core `c877d92f6` (lively = what ships; chance with 17 labels is 6 % top-1 / 18 % top-3):

| | Gemini 3.1 Pro, video | GPT-5.5, strip |
|---|---|---|
| hand-written presets: top-1 / top-3 / p(target) | 0 % / 38 % / 11 % | 23 % / 38 % / 12 % |
| hand-written presets: description at least related | 38 % | 58 % |
| planner on the preset prompts: top-3 / related | 54 % / 33 % | 38 % / 50 % |
| planner on 30 held-out prompts: top-3 / related | 26 % / 42 % | 52 % / 77 % |
| alive 1-5, lively / direct (presets) | 2.62 / 2.50 | 2.90 / 2.81 |
| A/B lively wins (186 single-order verdicts, before the both-orders A/B) | 46 % (the second-shown video wins 77 %) | 56 % (91 % "slight") |

Planner (`gpt-6-astra`, 190 writes): 100 % valid on the first pass, no repairs, 4.9 s median per call
(p90 8.1 s). Physical probes, 18 × 8 samples: 97 % (out-of-distribution core 100 %).

What the failures say (details in the report):

- **Two silhouettes.** Every positive preset swings the gripper 25-40 cm out of the fold; every
  negative one keeps it within 5 cm. The judges name the shape: half (Gemini) to nearly all (GPT-5.5)
  descriptions of unfolding clips are "reaching / pointing / waving / raising a hand", so happy reads
  as a startle, angry as presenting or waving, and scared and sleepy as a slump or "powered down". The
  biggest lever is the basis: visible negative ends (a dropped elbow, a downturned claw, the arm
  pulled back) and distinct positive ones (a mast, out to the side, low toward the person).
- **Fast detail never reaches the body.** The 2 Hz serving low-pass with 0.25 s keys keeps 9 / 23 /
  61 % of an `osc` at a 0.3 / 0.4 / 0.6 s period; 55 of the planner's 81 `osc` are faster than
  0.7 s. The speed limits halve 0.15 s snaps (a sneeze release commands 12.8 rad/s; it plays at 6).
- **Liveliness is not measurably more alive** to these judges; their A/B is dominated by order.
- **Live stack (sim):** a reply emote's generated clip starts ~2 s after the line (1.8-2.5 s),
  and the instant stand-in preset is `curious` for 8 of 11 body-language prompts. `/brain/tts` now
  plays emote tags (fixed during this eval, 3251ea373).
- **Probe bug:** `probes._half_cycles` misses two-shake head shakes (a window that grows with the
  clip, and L-R-L-R gives 3 sign changes where the check needs 4).

### Demo videos

`expressive/demo/`: MARS explains the harness in its own voice, as Binh's reachy-explain did. A
`show.yaml` of beats (`say`, `emote` or `recipe`, `pause`, `sync: release`) is spoken with macOS `say`.
Its emotes are planned (cached) or scripted, and the whole show plays through ONE offline `Animator`
ticked at 30 fps (`feed_speech` at each line, `play` for each gesture, idle breathing between). It is
filmed physically in the sim with subtitles, and the speech is muxed in.

```bash
uv run mars-express show demo/show.yaml out/demo/mars_explains.mp4   # 62 s, 13 beats
uv run mars-express demo        # + out/demo/idle_speech.mp4 (20 s) and out/demo/presets_montage.mp4 (15 s)
```
