# MARS on LeRobot

`lerobot_robot_mars` makes the Innate MARS a [LeRobot](https://github.com/huggingface/lerobot)
robot. Once installed next to lerobot, `--robot.type=mars` works in `lerobot-teleoperate`,
`lerobot-record`, `lerobot-replay`, and `lerobot-rollout`, and `mars2lerobot` exports skills
recorded by the Innate app to a LeRobotDataset v3.

The lerobot process never talks to ROS. It talks to a small bridge inside `manipulation_server`
on the robot over two ZMQ sockets; the bridge is on by default and idles until a client
connects. That process can run on a laptop (`--robot.remote_ip=mars.local`) or on the Jetson
itself in this package's own Python 3.12 environment (`--robot.remote_ip=localhost`).

## Install

lerobot main needs Python 3.12; the robot's ROS stack is Python 3.10. Keep the two apart with
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
uv run lerobot-teleoperate --robot.type=mars --robot.remote_ip=localhost \
    --robot.external_commands=true --teleop.type=mars_passthrough --teleop.remote_ip=localhost
```

Drive the sim from the web app; the terminal shows lerobot observing the commanded joints.
Every command below works against the sim the same way with `remote_ip=localhost`.

## Record with the Innate app driving

The passthrough teleoperator reads back whatever the app, the leader arm, or a skill last
commanded, so recording needs no new teleop hardware. Pair it with `external_commands=true`
so the client never re-sends that command behind the operator:

```bash
uv run lerobot-record \
    --robot.type=mars --robot.remote_ip=mars.local --robot.external_commands=true \
    --teleop.type=mars_passthrough --teleop.remote_ip=mars.local \
    --dataset.repo_id=innate/mars-tidy-up --dataset.single_task="Put the ball in the box" \
    --dataset.fps=30 --dataset.num_episodes=10 --dataset.push_to_hub=false
```

Recording keys are lerobot's: right arrow ends an episode, left arrow re-records it, escape
stops. lerobot appends a date-time stamp to the repo id unless you pass
`--dataset.no_stamp=true`. The dataset lands under `$HF_LEROBOT_HOME/<repo id>` (override with
`--dataset.root=...`); add `--dataset.push_to_hub=true` after `huggingface-cli login` to publish it.

## Replay and policies

```bash
uv run lerobot-replay --robot.type=mars --robot.remote_ip=mars.local \
    --dataset.repo_id=innate/mars-tidy-up --dataset.episode=0
uv run lerobot-rollout --strategy.type=base --policy.path=<checkpoint> \
    --robot.type=mars --robot.remote_ip=mars.local --task="Put the ball in the box"
```

Without `external_commands`, `send_action` forwards six absolute joint targets (rad) to
`/mars/arm/commands` and the base twist to `/cmd_vel_skills`. The bridge ignores commands while
an Innate behavior is executing, and stops the base if a commanding client goes silent for
half a second.

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
- The robot ignores `send_action` while an Innate skill or policy is executing (`busy` in the
  state header); wait for it to finish.
- The base stops half a second after the last action: that is the watchdog. Keep sending
  actions at the control rate, as `lerobot-record` and `lerobot-rollout` do.
