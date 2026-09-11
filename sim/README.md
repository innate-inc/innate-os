# Innate Simulator

<p align="center">
  <a href="https://sim.innate.bot"><img src="../docs/assets/readme/sim.png" alt="Driving the simulated MARS robot through its apartment in the browser" width="85%"></a>
</p>

**[Try MARS now at sim.innate.bot](https://sim.innate.bot)** — no install or robot required.

A digital twin of [MARS](https://innate.bot) that runs on your laptop. The same navigation, skills, agent, and web app as the physical robot, against a MuJoCo apartment. Anything you build here runs unchanged on a real MARS.

## Run it locally

One command on macOS, Linux, and WSL2:

```bash
curl -fsSL https://link.innate.bot/sim | sh

cd innate-os
./innate-sim up
```

Open [https://localhost](https://localhost) (accept the self-signed certificate). Drive with the joystick or WASD, run skills from the web app, and talk to the agent.

Already have this checkout? `sh scripts/install-sim.sh` from the repo root. OS-specific install, agent keys, and troubleshooting are in the [setup docs](https://docs.innate.bot/simulator/setup).

```bash
./innate-sim status
./innate-sim sh
./innate-sim logs startup
./innate-sim down
```

16 GB of RAM and 4 or more cores is comfortable. The first start downloads a few GB; later starts take seconds.

## Build against it

The container mounts this repository, so `workspace/` is the same folder a real robot uses. Skills and agents hot-reload on save. Write them there, trigger them from the web app, then deploy the same files to hardware.

- [Building with the simulator](https://docs.innate.bot/simulator/building-with-the-simulator)
- [Skills](https://docs.innate.bot/software/skills)
- [Agents](https://docs.innate.bot/software/agents)

| You edited | What to do |
|---|---|
| skills or agents in `workspace/` | nothing — they hot-reload on save |
| parameters in `config/` | inside the container: `innate restart` |
| ROS code in `ros2_ws/src/` | inside the container: `innate build` |
| the simulated world, challenges, or props | `./innate-sim down && ./innate-sim up` |
| launcher / webapp files | rerun `./innate-sim up` / reload the browser |

### Challenges

Scored tasks in the web app's Agent page. Pick one — it resets the world, drops the props the scenario needs, and ticks a goal checklist. The **world server** judges from MuJoCo state, not from what the robot reports.

A challenge is a sidecar in [`sim/challenges/`](challenges/):

```python
from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

CHALLENGE = Challenge(
    id="shepherd",
    title="Shepherd",
    brief="A soccer ball is lying in the apartment. Find it and push it to the dog.",
    setup=[Drop("soccer_ball", -4.69, 1.29), Drop("labrador", -0.49, 2.89, yaw_deg=90)],
    goals=[
        Goal("Get to the ball", Near("robot", "soccer_ball", 0.8)),
        Goal("Push it to the dog", Near("soccer_ball", "labrador", 1.2)),
    ],
    time_limit_s=600,
    environments=("apartment",),
)
```

Results persist in `workspace/challenges.json`. See `sim/challenges/40_household_orders/` for a challenge with its own runtime.

### Environments

The apartment is the default. Switch at launch or from **Scene setup → Environment** in the 3D view:

```bash
./innate-sim up --environment backrooms
```

Packs live in `sim/environments/`. Licensed packs the repository must not ship go in `sim/environments.local/` (gitignored). How to build a pack is in [`sandbox/README.md`](sandbox/README.md).

## VirtualMars

For scripts, notebooks, and RL loops there is a second way in that needs no ROS and no Docker: the apartment, the robot, its cameras, lidar, and arm as a Python object.

Start with [`sandbox/virtual_mars_demo.ipynb`](sandbox/virtual_mars_demo.ipynb):

```bash
./innate-sim assets
cd sim && uv sync --group notebook
```

```python
from mars_sim_driver.core import VirtualMars

sim = VirtualMars()
sim.step(1.0)
sim.set_cmd_vel(0.3, 0.5)
sim.set_joint_target("joint2", -1.0)
x, y, yaw = sim.pose()
rgb = sim.render_rgb("main")
depth = sim.render_depth("main")
scan = sim.lidar_scan(360, 12.0)
grid, ox, oy = sim.occupancy_grid()
sim.reset()
```

For a native MuJoCo window: `cd sim && uv run sandbox/drive_mars.py`. More tooling is in [`sandbox/README.md`](sandbox/README.md).

## Architecture

Four layers in `ros2_ws/src/mars_bot/mars_sim_driver/`. Each works without the ones above it:

```
node.py          mars_sim_driver — ROS 2 client impersonating the hardware drivers
world_server.py  world host      — owns the world + clock; driver RPC + observer stream
core.py          VirtualMars     — physics + sensors, no ROS
world.py         model building  — MJCF world + URDF robot, pure functions
```

The world always runs on the **host** (via `uv`), not in Docker: native GL is much faster, and physics does not compete with the ROS stack. The container's driver node is a pure RPC client. `./innate-sim logs world-server` is that process.

Robot software sees the world only through the driver. Humans and tools see it only through the observer stream (`/worldstate`) — scenery, props, and challenge scoring, never robot control. That split is why a challenge can judge honestly: the robot stack is never told one exists.

`node.py` publishes the same topics, types, rates, and frame names as the real drivers, so Nav2, the brain, the web app, and Foxglove run unmodified. The topic surface is in the `node.py` module docstring.

## Foxglove and ROS

The sim starts a Foxglove bridge for you. Connect [Foxglove Studio](https://foxglove.dev) to `ws://localhost:8765`. Because this is localhost, full-resolution cameras and point clouds are fine — unlike a physical robot on Wi-Fi, where you want the `/mars/main_camera/remote/*` topics. See the [Foxglove docs](https://docs.innate.bot/software/foxglove-setup).

A rosbridge server is at `ws://localhost:9090`.

## Working on the robot code

`./innate-sim sh` drops you into the container. The checkout is bind-mounted, so host edits are visible immediately. `innate build` rebuilds `ros2_ws/` (a `colcon` install is a copy — editing a node and restarting is not enough). `innate view` attaches the tmux session, one window per subsystem.

The trap is `mars_sim_driver`'s world server: it runs on the host and imports straight from the checkout. `innate build` does not reload it. Restart from the host: `./innate-sim down && ./innate-sim up`.

**Two checkouts at once.** Each checkout already has its own container and volumes. Ports belong to the machine — move them with `INNATE_SIM_PORT_BASE`:

```bash
INNATE_SIM_PORT_BASE=8600 ./innate-sim up   # web app at https://localhost:8600
```

Export it for every `./innate-sim` command in that checkout (`down`, `status`, and `logs` look for the same ports).

| Offset | Service | Override | Default |
|---|---|---|---|
| +0 | webapp (https) | `SIM_HTTPS_PORT` | 443 |
| +1 | webapp (http) | `SIM_HTTP_PORT` | 80 |
| +2 | rosbridge | `SIM_ROSBRIDGE_PORT` | 9090 |
| +3 | leader receiver (UDP) | `SIM_UDP_PORT` | 9999 |
| +4 | Foxglove bridge | `SIM_FOXGLOVE_PORT` | 8765 |
| +5 | world server RPC | `SIM_WORLD_PORT` | 8799 |
| +6 | world state stream | `SIM_WORLD_STATE_PORT` | 8800 |

## Configuration

- repo-root `.env` — secrets; `./innate-sim setup` walks through them
- `config/settings.yaml` — optional ROS tunables
- `sim/config.toml` — optional overrides, created from `config.toml.template`
- `INNATE_SIM_RENDER_SCALE=N` — render cameras at 1/N (helps software rendering)

## Credits

Crossroads and the traffic cars are original Innate geometry, generated from primitives. The other environments and characters are third-party works under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): the apartment by [SrMonteiro](https://sketchfab.com/crispimrafael), the backrooms by [carlcapu9](https://sketchfab.com/carlcapu9), the scenario human and the three household residents by [restore50](https://sketchfab.com/restore50), and the dog by [all of life](https://sketchfab.com/Xfdfgd).

[`ATTRIBUTION.md`](ATTRIBUTION.md) is the canonical list — per-asset sources and the changes made to each. It ships inside the published asset and viewer images alongside the geometry it covers.
