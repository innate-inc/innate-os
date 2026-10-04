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
- A lowered gaze brings the body with it (`couple`, applied to every row before the safe table, so
  planner clips get it too): as attend goes from −0.3 to −1 the arm slumps by up to rise −0.6 and
  approach −0.3 (claw turned down), and above attend +0.5 it lifts by up to rise +0.25. Seen from
  above, a head tilted down shows more of its flat top and alone reads as looking *up*; below the
  thresholds every channel stays linear.
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

- a running skill (a `running` on `/brain/skill_status_update`, or the brain's own skill slot)
  masks the parts it owns, and a live Nav2 `/navigate_to_pose` goal masks everything. A masked
  part's output stops at once and resumes from wherever the skill left it; the other parts keep
  breathing and swaying (`turn_in_place` leaves the arm and head expressive). Masking is silence
  rather than `Animator.set_mask`, whose eased-to-NEUTRAL parts would start the resumed stream at
  NEUTRAL instead of the measured pose. The skills server puts the parts a skill's declarations
  reach in its `running` status as `body` (`Head` → head, `Manipulation` → arm, `Mobility` → base,
  plus its subskills' parts; physical subskills → all), and `expressive_driver/utils.py` decides:
  `LEAVES_BODY` (`search_memory`, `change_volume`, `express`, `head_emotion`, which moves the body
  through this driver) masks nothing; `MASKS_ALL` (`navigate_to_position`, `navigate_with_vision`,
  which drive the base through raw ROS clients without declaring `Mobility`) masks everything;
  a skill that declares no body part, or a run with no `body`, masks everything too, since a
  declaration cannot prove a skill body-free. Add a shipped skill to the right list when its
  declarations under- or over-state what it moves;
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
  report commanded joints) keeps its grip;
- joints in `expressive.hold_joints` (a faulty servo on one unit, e.g. `["j4"]`) stay at the angle
  measured when the stream starts, in every command, while the rest of the arm plays. j1 and j2 are
  held together, since the shoulder clearance couples them. Changing the list restarts the stream
  from the measured pose, so a released joint blends back instead of jumping.

Eye contact (`expressive.camera_clear`): while the gaze tracker runs, or a face was seen in the last
5 s, the clips the layer makes (prompts, presets, emotes, reactions; not a clip sent to
`/brain/express/play`) are capped where the arm would cross the middle of the head camera's image,
where the tracked face sits: approach ≤ 0.3, expand ≤ 0.1, rise ≤ 0.15, attend ≤ 0.3
(`CAMERA_CLEAR_CAPS` in `expressive_driver/utils.py`). The decision is made per clip, when it is
prepared; the next clip after nobody is watched gets its full excursions. Rise is capped, not
scaled: rendered from the camera, the arm covers 40 % of the middle box at rise 0.3-0.4 (half-way
up, in front of the lens) and only 5 % at rise 1, so scaling rise by 0.6 parks the arm right in
front of the face (the presets' mean coverage of the middle went up, 25 → 32 % for `excited`).
Over every preset, frames covering more than 10 % of the middle drop from 27 % to 2 %. In the sim,
with the gaze tracker on, `happy`, `proud` and `excited` covered 3.5 % of the middle on average
(at most 11 %, no pose above 10 %), against 25 % (at most 43 %, 92 % of poses above 10 %) with
`camera_clear` off. The price: the arm stays low while someone is watched (no raised-arm joy or
pride); head, base and grip still play.

Speech sway (only while an agent runs, or on top of a playing clip): the TTS loop hands the
robot's own voice (never the sim's simulated residents) to
`Animator.feed_speech` as it reaches the speaker: PCM s16le 16 kHz chunks on hardware, and in the
sim the whole 44.1 kHz WAV once, when it is published on `/tts/audio`. The sway lays the audio
end to end at playback speed (32,000 B/s), however fast it streams in. On hardware the first 0.5 s
of each utterance is held back until `aplay` would start the device (its default start threshold
is a full 0.5 s buffer), and every chunk is flushed to `aplay` as it arrives. Offline, against a
fake `aplay` that plays like ALSA, this puts the sway on the speaker within 1 ms with the stream at
4.5x or 1.3x real time; before, the sway led the voice by the buffer's fill time (110 ms at 4.5x,
407 ms at 1.3x). `expressive.speech_latency_s` shifts it if a real device differs.

Vocalizations (`expressive.vocalize`): a prompt or preset that plays while the robot is silent
(no speech playing or queued) also makes a short non-verbal sound — a gasp, sigh, chirp, hum,
grumble, yawn or chuckle, picked by keyword from the prompt, else from the preset it matched
(`expressive_driver/vocal.py`; a prompt with neither stays silent). The sounds are synthesized in
numpy (tones and band-passed breath, 0.3-1.3 s, three variations each, cached at startup) at
12 dB under the voice (-40 dBFS RMS against Cartesia's measured -28). They go through the TTS
queue, so they never overlap speech: a reply's first sentence drops a sound still queued, and a
sound playing finishes before the reply starts. On hardware they play through the same `aplay`
as speech; in the sim they are published on `/tts/audio` as a 16 kHz WAV. They raise
`/tts/is_playing` (the microphone ducks) and feed the speech sway like the voice does. Emotes in
a reply are never voiced: their sentence is about to be spoken.

### ROS surface

| topic | type | |
|---|---|---|
| `/brain/express/prompt` | String | a prompt, `{"prompt", "id"}` to generate and play, or `{"preset", "id"}` to play a built-in preset at once |
| `/brain/express/play` | String | a Clip JSON object to play now (never a path; malformed clips are logged and ignored) |
| `/brain/express/stop` | String (payload ignored) | back to idle. Not `Empty`: rosbridge (rws) serializes `std_msgs/Empty` to 0 bytes, which every ROS subscriber rejects |
| `/brain/express/state` | String, 5 Hz | `{playing, name, t, duration, idle, masked, masked_parts, camera_clear, speaking, source, id}`; `masked_parts` lists the parts a skill or Nav2 holds; `camera_clear` is true while someone is watched and clips are capped |
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
| `expressive.vocalize` | `true` | a prompt or preset played while the robot is silent makes a short non-verbal sound; applies at once |
| `expressive.enabled_parts` | `[arm, base, head]` | the parts the layer may move; the rest stays still. Applies at once (`ros2 param set /brain_client_node expressive.enabled_parts "[head]"`): the bring-up stages it |
| `expressive.speech_latency_s` | `0.0` | how far the speaker trails the speech sway's audio; positive delays the motion |
| `expressive.camera_clear` | `false` | opt in to cap the arm out of the head camera's view while someone is watched (above); off by default because it removes raised-arm gestures exactly when a person is present; applies at once |
| `expressive.hold_joints` | unset (none) | arm joints held at their measured angle, e.g. `["j4"]` for a faulty servo; applies at once (`ros2 param set /brain_client_node expressive.hold_joints "[j4]"`; `"['']"` clears it, since the CLI cannot send an empty list) |
| `expressive.server_url` | `""` | the planner server (`http://<host>:8000`); empty skips it. Set it per robot in `config/settings.yaml` (stanza below) |
| `expressive.on_skill_completed` | `agreeing` | a preset name plays instantly; other text is a prompt to generate; `""` for none |
| `expressive.on_skill_failed` | `sad` | |

The agent emotes through tags in its replies. The system prompt asks for
`<emote>a feeling plus one physical cue, 2-8 words</emote>` at the start of a reply and on
emotional beats, invented fresh each time (its examples span proud, sheepish, startled and subtle,
and it is told never to reuse one: with a single example the model copied it verbatim); the
speech streamer cuts them out (never spoken, never shown in the chat) and plays each one when its
sentence goes to TTS. Skills call `express(prompt, wait=True)` (`workspace/innate_skills/express.py`);
`head_emotion` keeps its 13 emotion names but plays each through this driver with the whole body
(a preset, or a prompt for `very_happy` and `disappointed`), and the agents' prompts now ask for
emote tags while talking and `head_emotion` only for a deliberate gesture.

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

### Hardware bring-up

`scripts/expressive_bringup.sh` brings the layer up on a real robot one part of the body at a
time, in about ten minutes. Run it on the robot with the agent stopped (Agent page) and no skill
running; it sources the ROS environment if the shell has not. Each stage sets
`expressive.enabled_parts` (and `expressive.vocalize` for `voice`), drives the driver through its
topics, measures what reached the servos, the speaker and the base, and prints PASS or FAIL for
every check with its number. On exit, Ctrl-C included, it stops the expression, sends a zero twist
after `full`, and restores the parameters. Each run leaves `/tmp/expressive_bringup/<stage>-<time>.log`
and `.json` (the raw samples).

```bash
cd ~/innate-os
scripts/expressive_bringup.sh check   # 10 s
scripts/expressive_bringup.sh head    # 40 s, watch the head
scripts/expressive_bringup.sh voice   # 45 s, listen
scripts/expressive_bringup.sh arm     # 30 s; --rounds 6 is a three-minute thermal soak
scripts/expressive_bringup.sh full    # 45 s, the base too, once the operator types YES
```

Run them in this order and stop at the first FAIL.

| stage | moves | plays | passes when |
|---|---|---|---|
| `check` | nothing | — | the brain node is up; `expressive.enabled` on, `simulator_mode` off, `expressive.enabled_parts` declared; the state at 4-6 Hz; `/joint_states`, `/mars/arm/state`, `/odom` above 5 Hz; the prompt, stop, `/brain/tts`, head, arm and cmd_vel topics each have a subscriber, `/tts/is_playing` a publisher; `/mars/arm/status` ok with torque on; the agent stopped and nothing masked; the planner server's `/health` answers (a robot with no server URL is only noted) |
| `head` | head | `curious`, `agreeing`, `sad`; `listening` silent, then over a spoken line | at least 5 head commands per preset; within ±20°; at most 31 commands in any second; every step within the 200°/s cap (+1° for rounding); the measured head follows the commands with at most 0.3 s lag and 4° p95 error; 0 arm commands and 0 twists; the spoken line moves the head at least 0.3° RMS away from the silent take; the state reports `speaking` at least 80 % of the time from the first sway until the voice stops |
| `voice` | head | `surprised` and `sleepy` on a silent robot; `happy` during a line; `surprised`, with a line sent while its gasp plays | each silent emote raises `/tts/is_playing` once, within 0.5 s, for its sound's length ± 0.3 s (gasp 0.40 s, sigh 1.10 s); the emote during speech adds no second run; the line starts only after the gasp ends; the operator confirms the three sounds were audible, clearly under the voice, with no clicks |
| `arm` | arm, head | `agreeing`, `curious`, `happy`, `proud`, `excited`, × `--rounds` | j1-j5 load at most 70 % (`/mars/arm/state` effort); `/mars/arm/status` ok throughout; every streamed step within `basis.json` `max_speed` × the ticks elapsed (× 1.15: message arrival jitters a few ms around 33 ms); 0 twists; torque on and status ok afterwards |
| `full` | everything | `confused`, `curious`, `sad`, `affectionate`, `scared` (up to 46° and 25 cm) | for each: at most 50 cm from the anchor; back within 3 cm and 3° after 2.5 s; twists within 0.165 m/s and 0.66 rad/s; the last twist is zero and the base is silent for the final second; the `arm` checks throughout |

In `arm` and `full`, a j1-j5 load over 70 % for 0.1 s, or any not-ok `/mars/arm/status`, stops the
expression at once and fails the stage after reporting the arm checks and the torque state. The
claw (j6) reports current, not load, so its peak is only noted. `--ignore-load j4` leaves a joint
whose load reading is broken out of the load abort; a joint in `expressive.hold_joints` is left out
of the step check and instead must not move at all in the commands (its measured range is noted).
Also noted without a threshold:
the head servo's lag, how long after `/tts/is_playing` the sway starts (synthesis plus `aplay`'s
start buffer), the arm stream's rate, and its tracking error per joint at +150 ms.

What the script cannot judge:

- servo temperature below 70 °C. The arm node publishes temperatures only as its own ≥ 70 °C
  (and ≥ 80 % load) flag in `/mars/arm/status`, every 5 s, which aborts the stage; a 60 °C limit
  needs the arm node to publish temperatures. Feel the servos after `arm --rounds 6`.
- whether the sway keeps time with the voice: watch the head during `head`'s line, and set
  `expressive.speech_latency_s` (restart the brain) if it leads or trails.
- how loud the sounds are: the operator answers in `voice`; `BELOW_VOICE_DB` in `vocal.py` sets it.

In the sim, `check` fails `simulator_mode` and `/mars/arm/status` (the sim publishes none). The
other stages run once a status is faked:
`ros2 topic pub -r 1 /mars/arm/status mars_msgs/msg/ArmStatus "{is_ok: true, is_torque_enabled: true}"`.

Still checked by hand:

- [ ] the arm streaming at TELEOP gains for minutes while an agent runs: whether the rest fold
      should be skipped when expression owns the idle arm
- [ ] `basis.json` `max_speed` (the TELEOP profile speeds: j1 6.0, j2 3.6, j3 4.8, j4 2.4, j5 2.4,
      j6 1.4 rad/s) against the pass-through's soft gains; `arm`'s tracking errors are the evidence
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
# per judge: gemini (Gemini 3.1 Pro watches each video, via the Innate proxy) or openai (GPT-5.5 reads each strip)
uv run --extra flow mars-express eval --judge gemini --n 3 \
    --flow out/models/generator.pt --pair flow,lively --pair lively,direct
uv run --extra flow mars-express eval --judge gemini --n 3 --camera human --only 'preset-*' \
    --arms lively,flow --flow out/models/generator.pt         # the head-height camera, cached apart
uv run --extra flow mars-express eval --judge gemini --n 3 --audio --only 'preset-*' \
    --arms lively,flow --flow out/models/generator.pt         # with the robot's vocalizations, cached apart
uv run mars-express interaction chat_demo.mp4             # a recorded conversation, rated reply by reply
uv run mars-express eval --report-only --snapshot eval/   # rebuild REPORT.md + the committed copy
```

- **Prompts.** The 17 presets twice (the hand-written recipe and one the planner writes for the same
  prompt), and 30 held-out prompts no preset covers (embarrassed, disgusted, a snake rearing up, a
  grumpy neighbour, a cat hunting a mouse, tipsy, winning the lottery, crossing a street, the cookie
  jar…). The planner is `gpt-6-astra` through `planner.write`.
- **Arms.** Each recipe is played three ways. **lively** is procedural liveliness on the expanded
  recipe, exactly `Clip.from_recipe`: what ships. **direct** is the recipe played as written (the
  control). **flow** is the ml workstream's flow-matching generator (`--flow CKPT`, torch via
  `--extra flow`), conditioned on the sparse serving plan as served. Any other `(expanded recipe,
  seed) -> motion` function joins with `--arm name=module:function`.
- **Media.** A 2x4 key-frame strip (farthest-point sampled, so a 0.2 s snap makes the strip) and a
  caption-free mp4 played physically in the sim, both from the three-quarter view.
- **Judges.** Three independent calls per clip. Each spreads probability over the studio's 17 labels
  (read from `webapp/js/expression/judge.js`, so the two judges cannot drift), gives one free-text
  description, and rates alive and readable 1-5. A text grader then scores each description against
  the prompt (2 same, 1 related, 0 different).
- **A/B.** A pairwise judge picks the more alive of each `--pair` (repeatable). Every round shows the
  pair in both orders, and a win counts only when both orders agree. A round where the same slot won
  both times counts as position-biased.
- **Planner.** First-pass validity, repairs and latency of every write, and the 18 physical probes ×
  8 samples.
- Everything is cached under `out/eval/`. A clip that changes (a new core, a retuned basis) drops its
  media and judgments, a changed prompt is planned again, and the core commit is recorded with the run.

Results on core `10d0232cc` (basis v6, retuned presets). Chance with 17 labels is 6 % top-1 and 18 %
top-3. "related" is the share of blind descriptions graded at least related to the prompt.

| judge | prompts | arm | top-1 | top-3 | related | alive 1-5 |
|---|---|---|---|---|---|---|
| GPT-5.5, strip | 17 hand-written presets | lively | 43 % | 64 % | 73 % | 2.92 |
| | | direct | 14 % | 43 % | 57 % | 2.90 |
| | | flow | 21 % | 50 % | 59 % | 2.84 |
| | planner, preset prompts | lively / direct / flow | 21 / 21 / 14 % | 43 / 50 / 36 % | 55 / 53 / 51 % | 2.90 / 2.92 / 2.98 |
| | planner, 30 held-out | lively / direct / flow | 30 / 22 / 22 % | 52 / 48 / 48 % | 72 / 71 / 68 % | 3.08 / 3.04 / 3.00 |
| Gemini 3.1 Pro, video | 17 hand-written presets | lively | 7 % | 36 % | 45 % | 2.47 |
| | | direct | 14 % | 36 % | 31 % | 2.57 |
| | | flow | 29 % | 57 % | 37 % | 2.49 |
| | planner, preset prompts | lively / direct / flow | 7 / 7 / 29 % | 36 / 14 / 50 % | 43 / 27 / 45 % | 2.43 / 2.37 / 2.41 |
| | planner, 30 held-out | lively / direct / flow | 19 / 19 / 11 % | 44 / 44 / 48 % | 46 / 39 / 43 % | 2.68 / 2.58 / 2.63 |

A/B, 192 rounds per pair and judge, each judged in both orders:

| judge | pair | agreed rounds | position-biased (first / second slot won both) |
|---|---|---|---|
| GPT-5.5 | flow vs lively | flow 73, lively 37 (flow 66 %) | 12 / 70 |
| GPT-5.5 | lively vs direct | lively 52, direct 61 (lively 46 %) | 24 / 55 |
| Gemini | flow vs lively | flow 34, lively 12 (flow 74 %) | 7 / 139 |
| Gemini | lively vs direct | lively 34, direct 32 (lively 52 %) | 13 / 113 |

Planner (`gpt-6-astra`, 191 writes): 100 % valid on the first pass, no repairs, 4.9 s median per call
(p90 8.0 s). Physical probes, 18 × 8 samples: 99 % (out-of-distribution core 100 %).

What the results say (details in the report):

- **The video judge reads gaze down as head up.** From the elevated three-quarter camera, a head
  tilting down shows its flat top and its silhouette grows. In 23 of 30 Gemini readings of gaze-down
  clips the head "jerks up"; GPT-5.5, reading stills, says down 17 times out of 18 head mentions. Every
  slump becomes a startle on video, so the negative presets fail there. The head needs a visible face,
  and the judge video wants a camera nearer the head's height.
- **Camera height.** Re-judging the 17 presets (lively and flow) with Gemini from a head-height
  camera (`--camera human`: eye 0.41 m, 1.5 m away, looking down 8°, 30° off heading) doubles "head
  down" for the gaze-down presets, from 8 to 15 of 30 readings, while 16 still say up. Recognition
  drops, though: lively top-3 36 → 14 %, flow 57 → 21 %, and "neutral" becomes the commonest label as
  the arm's shapes flatten into the body. The elevated view stays the default; the gaze needs a face
  on the head, not a lower camera. That run is cached apart (`strips@human/`, `videos@human/`,
  `judged/<judge>@human/`), so the numbers above stand.
- **Sound.** The same 17 presets were re-judged by Gemini with the robot's vocalization muxed in
  (`--audio`: `expressive_driver.vocal`'s choice, 12 dB under the voice). Alive rises 2.47 → 2.75
  (lively) and 2.49 → 2.84 (flow), but recognition stays within noise. Lively top-1 / top-3 / related
  goes 7 / 36 / 45 % → 7 / 36 / 39 %, flow 29 / 57 / 37 % → 14 / 50 / 41 %. The judge names the sound
  in 47 of 102 readings' cues but in only 4 descriptions. The gasp and the sigh help (surprised
  reads "startled" every time; p(sad) 0.15 → 0.37). The yawn hurts: sleepy becomes "howling like a
  wolf".
- **Interaction.** Gemini watched the 96 s live chat demo (screen and voice, no transcript) three
  times (`mars-express interaction chat_demo.mp4`). It rated "emotionally expressive while
  interacting" 4 / 4 / 4 and "movements fit the words" 4 / 4 / 4. Of 21 reply judgments, 16 fit,
  2 partly and 3 do not. Congratulations reads as excited, "I'm so sorry" as sad, "Hmm…" as
  thinking, and the goodbye as a wave. Its flags: a still-looking greeting (2 of 3 viewings), a laugh
  left in the previous sad pose (1), and the base turning after the conversation (2).
- **The reach silhouette.** 50 of 64 clips unfold the arm, and 93 % (GPT-5.5) and 57 % (Gemini) of
  their descriptions say reach, point, present or wave. Angry's forward lunge reads as "reaching out
  for a hug", and agreeing's nods with the arm held forward as "presenting something".
- **Flow beats lively on aliveness** in the agreed A/B rounds of both judges, and on video
  recognition of the presets. Its arm motion is smoother (35-45 % less detail above 1 Hz).
- **Liveliness against the bare recipe is a coin toss** on aliveness. It helps the strip judge's
  preset recognition (top-1 43 % against 14 %).
- **The Gemini A/B is mostly position:** the second-shown video won both orders in 113-139 of
  192 rounds. Only agreed rounds count.
- **Live stack (sim, earlier core):** a reply emote's generated clip starts ~2 s after the line
  (1.8-2.5 s). `/brain/tts` plays emote tags since 3251ea373.

### Demo videos

`expressive/demo/`: MARS explains the harness in its own voice, as Binh's reachy-explain did. A
`show.yaml` of beats (`say`, `emote` or `recipe`, `pause`, `sync: release`) is spoken with macOS `say`.
Its emotes are planned (cached) or scripted, and the whole show plays through ONE offline `Animator`
ticked at 30 fps (`feed_speech` at each line, `play` for each gesture, idle breathing between). It is
filmed physically in the sim with subtitles, and the speech is muxed in.

```bash
uv run mars-express show demo/show.yaml out/demo/mars_explains.mp4   # 62 s, 13 beats
uv run mars-express demo        # + out/demo/idle_speech.mp4 (20 s) and out/demo/presets_montage.mp4 (15 s;
                                # the six presets that read best blind: surprised proud excited curious sad sleepy)
```
