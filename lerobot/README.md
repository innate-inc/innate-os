# MARS on LeRobot

`lerobot_robot_mars` makes the Innate MARS a [LeRobot](https://github.com/huggingface/lerobot)
robot. Once installed next to lerobot, `--robot.type=mars` works in `lerobot-teleoperate`,
`lerobot-record`, `lerobot-replay`, and `lerobot-rollout`, and `mars2lerobot` exports skills
recorded with the Innate phone app or web app to a LeRobotDataset v3.

The lerobot process never talks to ROS. It talks to a small bridge inside `manipulation_server`
on the robot over two ZMQ sockets; the bridge is on by default and idles until a client
connects. That process can run on your computer (`--robot.remote_ip=mars.local`) or on the Jetson
itself in this package's own Python 3.12 environment (`--robot.remote_ip=localhost`).

## On the robot

The bridge ships with innate-os from this branch on; nothing extra runs on the robot. Update
it the usual way (`innate update`, or `innate update --dev apply <branch>` for a branch) and
check the manipulation server's log, in `innate view` or the webapp Logging page, for:

```
LeRobot bridge listening on :5555 (actions) and :5556 (observations)
```

If it says `LeRobot bridge disabled: …` the line names the reason; `innate update reinstall`
installs the missing Python dependency and rebuilds. Tell your computer the robot's hostname
once, the one you type into the browser for the web app:

```bash
export MARS_HOST=mars.local       # or mars-the-2nd.local, or an IP
```

Every command below reads it. `--robot.remote_ip=…` and `--teleop.remote_ip=…` override it,
which is how the same commands target the simulator with `localhost`.

## Install

On your computer. lerobot main needs Python 3.12; the robot's ROS stack is Python 3.10. Keep the two apart with
[uv](https://docs.astral.sh/uv/), which downloads its own interpreter:

```bash
cd innate-os/lerobot
uv sync            # creates .venv with lerobot 0.6 and this plugin, editable
uv run lerobot-record --help
```

Never `pip install lerobot` into the Jetson's system Python: it replaces the pinned numpy and
breaks the ROS image pipeline.

## Try it against the simulator

Start the simulator as usual (`./innate-sim up`; the container publishes the bridge ports
5555 and 5556 on loopback, or `INNATE_SIM_PORT_BASE + 7/8`), then from the same machine:

```bash
MARS_HOST=localhost uv run lerobot-teleoperate --robot.type=mars \
    --robot.external_commands=true --teleop.type=mars_passthrough --display_data=true
```

Drive the sim from the web app. `--display_data=true` opens Rerun with both cameras and the
joint plots; without it the terminal only prints the loop rate. Every command below works
against the sim the same way with `remote_ip=localhost`.

## Record while you teleoperate the usual way

Drive the robot however you normally do: the phone app, the web app's Teleop page, or the
leader arm. The passthrough teleoperator reads back whatever any of them, or a skill, last
commanded, so recording needs no new teleop hardware. Pair it with `external_commands=true`
so the client never re-sends that command behind the operator:

```bash
uv run lerobot-record \
    --robot.type=mars --robot.external_commands=true --teleop.type=mars_passthrough \
    --dataset.repo_id=YOUR_HF_NAME/mars-tidy-up --dataset.no_stamp=true \
    --dataset.single_task="Put the ball in the box" \
    --dataset.fps=30 --dataset.num_episodes=10 --dataset.push_to_hub=false
```

Recording keys are lerobot's: right arrow ends an episode, left arrow re-records it, escape
stops. lerobot appends a date-time stamp to the repo id unless you pass
`--dataset.no_stamp=true`. The dataset lands under `$HF_LEROBOT_HOME/<repo id>` (override with
`--dataset.root=...`); add `--dataset.push_to_hub=true` after `huggingface-cli login` to publish it.

To add episodes to an existing dataset, repeat the command with `--resume=true` and an explicit
`--dataset.root=$HOME/.cache/huggingface/lerobot/YOUR_HF_NAME/mars-tidy-up`; lerobot refuses to
resume without a root. `--dataset.num_episodes` then counts the episodes of this session.

## Replay and policies

```bash
uv run lerobot-replay --robot.type=mars \
    --dataset.repo_id=YOUR_HF_NAME/mars-tidy-up --dataset.episode=0
uv run lerobot-rollout --strategy.type=base --policy.path=outputs/train/act_mars/checkpoints/last/pretrained_model \
    --robot.type=mars --task="Put the ball in the box"
```

Without `external_commands`, `send_action` forwards six absolute joint targets (rad) to
`/mars/arm/commands` and the base twist to `/cmd_vel_skills`. Leave teleop first, in the phone app
and in the web app alike: while either is teleoperating it keeps publishing its own arm and base commands, the base mux
gives those priority, and the two arm streams fight. The bridge also ignores commands while an
Innate behavior is executing, and stops the base if a commanding client goes silent for half a
second.

## Trying a model that is only on lerobot main

New policies land on lerobot's `main` weeks before a PyPI release. The plugin only uses the
stable robot, teleoperator, and dataset interfaces, so it runs unchanged against `main`
(checked against 0.6.2 with the LaWAM adapter). Point this environment at main:

```bash
uv pip install --python .venv/bin/python \
    "lerobot[dataset,lawam] @ git+https://github.com/huggingface/lerobot@main"
```

Swap `lawam` for the extra of the policy you want, and pin a commit instead of `main` when
you need a reproducible run. `uv sync` puts the environment back on the released version.

## Publish from the web app

On the robot's Datasets page, a training dataset has a **Publish to Hugging Face** button. It converts the
skill's episodes to a LeRobotDataset on the robot and uploads them to the Hugging Face Hub:

1. Save a Hugging Face token with write permission under Settings, Keys.
2. Press Publish to Hugging Face on the dataset. The first time, the dialog offers to install the LeRobot
   environment on the robot, a one-time download of about 1.5 GB into `lerobot/.venv`
   (`uv sync --no-default-groups`, the lean set without training or viewer packages).
3. Pick the account or organization, a repository name, and whether it is private.

The job runs in the background at low priority and survives closing the dialog; reopening it
shows the progress. Publishing again converts only new episodes and uploads the dataset anew.
Under the hood it is the command below, run by the webapp server (`webapp/proxy/hub_publish.py`).

## Export recorded skills

```bash
uv run mars2lerobot ~/innate-os/workspace/custom_skills/pick_cube \
    --repo-id innate/mars-pick-cube --push --private
```

The converter reads `data/dataset_metadata.json`, skips episodes marked `failure` unless
`--include-failures`, takes the task text from the skill's guidelines (override with `--task`),
prefers the raw HDF5 in `raw_data/` and otherwise decodes the encoded MP4s, and records the
exported episode ids back into `dataset_metadata.json` so a rerun appends only new episodes.

## What a MARS dataset looks like

| Feature | dtype · shape | names |
|---|---|---|
| `observation.state` | float32 · (6,) | `joint1.pos` … `joint6.pos`, rad; joint6 is the gripper |
| `observation.images.head` | video · (480, 640, 3) | main camera, left eye, RGB |
| `observation.images.wrist` | video · (480, 640, 3) | arm camera, RGB |
| `action` | float32 · (8,) | `joint1.pos` … `joint6.pos`, `x.vel` (m/s), `theta.vel` (rad/s) |

The recorder's progress and termination columns are not stored; both are functions of
`frame_index` and episode length. `schema.py` is the single definition shared by the live
client and the converter.

**Head angle.** What the head camera sees depends on the head tilt, so it is fixed per dataset
and written to `meta/mars.json` (`head_angle_deg`), next to lerobot's own metadata and uploaded
with it. You normally never set it. When the client connects it works out the angle from the
dataset behind the run and holds the head there, commanding it back if someone tilts it:

- a new recording uses -20, the robot's "AI position", and writes that into the dataset;
- resuming or replaying a dataset uses that dataset's angle;
- running a policy uses the angle of the dataset it was trained on, which the checkpoint names
  in its `train_config.json`; the sidecar is read from the local cache or fetched from the Hub.

`--robot.head_angle_deg=<deg>` overrides all of that, and `--robot.hold_head=false` leaves the
head alone. The converter takes the angle the robot's recorder logged, or notes -20 as assumed.

## Protocol

Documented in `lerobot_robot_mars/wire.py` and pinned on both sides by `tests/test_wire.py`,
which runs the real bridge module from `ros2_ws/src/brain/manipulation` against the client.

```bash
uv run pytest            # plugin tests, including the bridge protocol
```

Bridge parameters live under `lerobot_bridge:` in `manipulation_server.yaml` and can be
overridden per robot in `config/settings.yaml`. The bridge needs `pyzmq` in the robot's system
Python (it is in `ros2_ws/pip-requirements.txt`); `manipulation_server` logs
`LeRobot bridge listening on :5555` at startup, or the reason it is disabled.

## Troubleshooting

- `No 'obs' messages from the MARS bridge …`: innate-os is not reachable at that address, the
  bridge is disabled, or the ports are not published (sim). Check the manipulation_server log.
- `torchcodec is installed but cannot be loaded` on macOS: harmless, lerobot falls back to PyAV.
  The `objc … AVFFrameReceiver is implemented in both` lines on macOS are harmless too.
- `zsh: no such file or directory: you`: a placeholder like `<you>` was pasted literally; the shell
  read the angle brackets as redirection. Placeholders in these docs are spelled `YOUR_HF_NAME`.
- The robot ignores `send_action` while an Innate skill or policy is executing (`busy` in the
  state header); wait for it to finish.
- Replay or rollout moves the arm oddly and the base not at all: the phone app or the web app's Teleop
  page is still teleoperating. Leave it before replaying.
- The base stops half a second after the last action: that is the watchdog. Keep sending
  actions at the control rate, as `lerobot-record` and `lerobot-rollout` do.
