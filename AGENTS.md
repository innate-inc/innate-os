# Innate OS — Agent Reference

## What is Innate?

Innate OS is a lightweight, agentic operating system for the **MARS** — a small mobile robot with an arm.
It runs on an **NVIDIA Jetson Orin Nano 8GB** (resource-constrained by design) and boots the full stack in under a minute.

The OS is built on **ROS 2 (Humble)** with **Zenoh** as the DDS transport, and natively supports agentic workflows, vision-language navigation (VLN), and vision-language-action (VLA) models via an on-robot agent loop that calls a hosted vision-language model.

For full documentation and hardware details, see [innate.bot](https://www.innate.bot/) and [docs.innate.bot](https://docs.innate.bot).

## System Modes

| Mode | Description |
|---|---|
| `hardware` | Running on the physical MARS robot |
| `sim` | Running in the software simulator (same ROS nodes, mock sensors) |

## CLI — `innate`

Run `innate` with no arguments to print the current system status (version, mode, ROS/DDS state).

| Command | Description |
|---|---|
| `innate view` | Attach to running nodes |
| `innate restart` | Restart ROS nodes |
| `innate build` | Build and restart |
| `innate build release` | Build release mode and restart |
| `innate skill list` | List available robot skills |
| `innate update` | Check and apply updates |
| `innate diag` | Check hardware diagnostics |
| `innate volume` | Get or set speaker volume |
| `innate --help` | Show all commands |

## Writing Skills

Skills live in `workspace/` (see [workspace/README.md](workspace/README.md)). A skill is a
`Skill` subclass; everything it consumes is declared with a type annotation.

### Never `time.sleep` — always `self.sleep`

**In skill code, use `self.sleep(seconds)`. Never `time.sleep(seconds)`.**

`self.sleep` wakes and raises `SkillCancelled` the moment a Stop lands; `time.sleep` blocks
to completion, so a skill that uses it keeps running (and keeps the robot moving) after the
user pressed Stop. Sleeping is the only cancel point a loop needs — write the loop as if
cancel didn't exist and let the framework halt the base and report `CANCELLED`.

```python
while traveled < target:
    self.mobility.send_cmd_vel(linear_x=velocity, duration=0.5)
    self.sleep(0.1)          # ✅ cancellable
    # time.sleep(0.1)        # ❌ Stop is ignored until the sleep finishes
```

`time` itself is fine for *measuring* — `time.time()` / `time.monotonic()` for deadlines and
elapsed checks. The rule is only about blocking.

Related cancel-aware helpers, all of which raise `SkillCancelled` too:

| Call | Use for |
|---|---|
| `self.sleep(seconds)` | Any pause in skill code |
| `self.wait_for(read, timeout)` | Block until a reader returns non-`None` |
| `self.check_cancelled()` | A checkpoint with no sleep (e.g. before an irreversible commit) |
| `self.cancelled` | Read the latch without raising |

Cleanup belongs in `try/finally` inside `execute()`. `self.on_cancel(hook)` is only for
forwarding a cancel to an external action goal — braking the base is automatic.

### The one exception: committed, non-cancellable sections

Teardown and already-committed physical actions must **not** be cancellable, so they use
`time.sleep` on purpose. Once `pick_any_object` closes the gripper, a cancel must not unwind
mid-grip and drop the object over the floor, so `_close_twist_lift` sleeps with `time.sleep`
and the run finishes carrying the object home.

If you write such a section, say so in a comment — otherwise the next reader "fixes" it back
to `self.sleep` and reintroduces the bug. Everywhere else, `self.sleep`.

## Key ROS Packages

| Package | Role |
|---|---|
| `mars_control` | Top-level app node, rosbridge websocket, teleop receiver |
| `mars_bringup` | Motor/IMU/LiDAR hardware drivers, TF tree |
| `mars_arm` | Arm + head servos, IK solver |
| `mars_cam` | Stereo cameras, depth estimation, WebRTC stream |
| `mars_nav` | Nav2 navigation, SLAM, mode manager |
| `brain_client` | Cloud brain bridge (STT/TTS, skills action server) |
| `manipulation` | Records/replays and runs manipulation policies |

Outside the ROS workspace, `lerobot/` is the LeRobot plugin (`lerobot_robot_mars`): a client for the
ZMQ bridge inside `manipulation_server`, a passthrough teleoperator, and `mars2lerobot`, which exports
recorded skills to LeRobotDataset v3. It has its own Python 3.12 uv environment; see `lerobot/README.md`.
