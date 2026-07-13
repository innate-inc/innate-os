# innate sim

Simulate the MARS robot on your laptop. There are two ways in, depending on
what you need:

| | You get | You need |
|---|---|---|
| **[1. VirtualMars](#1--virtualmars-the-sim-as-a-python-object)** | the whole simulated world as one Python object — scripts, notebooks, RL loops | `uv` |
| **[2. The digital twin](#2--the-digital-twin-full-robot-stack)** | the complete innate-os stack (Nav2, AMCL, brain, skills, webapp) running against the sim | Docker + `uv` |

Use VirtualMars to experiment with the robot's sensors and physics directly;
use the digital twin to build and test skills, agents, and input devices
exactly as they run on a real robot.

---

## 1 — VirtualMars: the sim as a Python object

No ROS, no Docker. The apartment, the robot, its cameras, lidar and arm live
in a single object you can import anywhere — and instantiate several of in
one process for parallel rollouts.

▶ **Start with the walkthrough notebook:
[`sandbox/virtual_mars_demo.ipynb`](sandbox/virtual_mars_demo.ipynb)** —
camera/depth/lidar observations, driving, the arm, and the occupancy grid,
with rendered outputs inline.

```bash
./innate-sim assets          # one-time: fetch the world geometry (~100 MB, no Docker)
cd sim && uv sync --group notebook   # then open the notebook on the sim/.venv kernel
```

The API is shaped like a robot, not a physics engine:

```python
from mars_sim_driver.core import VirtualMars

sim = VirtualMars()
sim.step(1.0)                          # settle from spawn; step(dt) runs physics
sim.set_cmd_vel(0.3, 0.5)              # vx m/s, wz rad/s (0.5s watchdog)
sim.set_joint_target("joint2", -1.0)   # arm/head PD servo setpoints
x, y, yaw = sim.pose()                 # ground truth
rgb   = sim.render_rgb("main")         # 640x480 ("wrist" = arm camera)
depth = sim.render_depth("main")       # meters; robot's own geoms excluded
scan  = sim.lidar_scan(360, 12.0)      # planar lidar off the visual surfaces
grid, ox, oy = sim.occupancy_grid()    # rasterized nav map (-1/0/100)
sim.reset()                            # back to spawn, arm home
```

Rendering is lazy — pure physics is cheap. For a native MuJoCo viewer window
(WASD driving, arm sliders in a browser control panel):

```bash
cd sim && uv run sandbox/drive_mars.py
```

More dev tooling (physics stress gate, asset pipeline) is documented in
[`sandbox/README.md`](sandbox/README.md).

---

## 2 — The digital twin: full robot stack

The real innate-os ROS 2 graph — brain, Nav2, AMCL, skills, webapp — runs in
Docker against the simulated robot. Only the hardware drivers are swapped for
a simulated equivalent; nothing above them can tell the difference.

```bash
./innate-sim setup     # one-time: prerequisites (installs uv if missing) + keys + brain backend
./innate-sim up        # host world server + container + ROS stack
```

The first `up` provisions everything (assets, Docker image, ROS build) and
later runs start in seconds. The world itself (physics + rendering) always
runs on the host via `uv` — never in the container, where software GL is
slow enough to break teleop. No Node.js required — the 3D viewer ships
prebuilt with the assets.

**Then open [https://localhost](https://localhost)** — the operator webapp
is the sim UI: drive with the joystick or WASD, watch the live 3D view
(main/arm cameras + orbit view + map), run skills, and talk to the agent,
exactly like on a real robot. Add `?simperf` to the URL for a frame-time /
latency HUD.

### Everyday commands

```bash
./innate-sim status      # startup checks + health snapshot
./innate-sim logs        # startup logs; `logs os` / `logs agent` follow live
./innate-sim sh          # shell into the container; `innate build` rebuilds ros2_ws
./innate-sim down        # stop
./innate-sim clean       # remove containers/volumes (keeps .env + config)
```

Local code edits + `innate build` (inside the container) behave exactly like
on a real robot. The container's ROS session lives in tmux
(`tmux attach -t innate`): one window per subsystem (zenoh, rosbridge,
sim-driver, nav-brain, behavior, arm-ik, vision-nav, console-webapp).

Prefer Foxglove? Open a Rosbridge connection to `ws://localhost:9090` for
TF, `/scan`, `/mars/main_camera/points`, camera topics and `/cmd_vel` teleop.

### Configuration

- repo-root `.env` — secrets (`INNATE_SERVICE_KEY`, brain backend keys);
  `./innate-sim setup` walks through them.
- `config/settings.yaml` — optional non-secret ROS parameter tunables and
  extra agent/skill dirs.
- `sim/config.toml` — optional overrides (OS image, cloud-agent mode),
  created from `config.toml.template` by setup.
- `sim/cloud-agent.lock` — the innate-cloud-agent revision this checkout is
  tested against: setup clones/aligns to it (and asks before touching an
  existing checkout); `up` only warns on mismatch, so forks and pinned
  experiments are never modified.
- `INNATE_SIM_RENDER_SCALE=N` — render the robot cameras at 1/N resolution
  (the wire format stays 640×480). On machines stuck with software rendering
  (`software-speed` in the dashboard's World field), `2` makes each frame
  ~4× cheaper and noticeably lowers end-to-end latency.

---

## How it works

The sim is a stack of four layers inside
`ros2_ws/src/mars_bot/mars_sim_driver/`; each is usable without the ones
above it:

```
node.py          mars_sim_driver -- thin ROS 2 client impersonating the hardware drivers
world_server.py  world host      -- owns the world + clock; driver RPC + observer stream
core.py          VirtualMars     -- the simulation itself (physics + sensors), no ROS
world.py         model building  -- MJCF world + URDF robot, pure functions
```

### world.py — the model

Builds the MuJoCo model: apartment collision hulls + textured visual rooms
(from `sim/assets`), the real `mars.urdf` attached on a planar base (x/y/yaw
— a wheeled robot can't pitch), drive gains and contact parameters. Pure
functions over files; `sim/sandbox` imports it for the native viewers, and a
future GPU/batched backend (e.g. MuJoCo Warp) would consume the same spec.

### core.py — VirtualMars

The layer you use directly in option 1 (see above). `update_camera()` /
`read_rgb()` are split (same for depth) so callers can update the scene under
a lock but render outside it — that's what keeps physics from stalling in the
world server.

### world_server.py — the world host

Hosts one VirtualMars behind two localhost interfaces, one per kind of
consumer (the invariant: robot software sees the world only through the
robot adapter; humans and tools see it only through the observer stream):

- **driver RPC** (port 8799) — sensing/actuation for node.py, robot-shaped
  and rate-limited like real hardware. Renders are demand-paced: a camera
  or depth product renders once per client pull (~8Hz), never free-running.
- **observer stream** (WebSocket, port 8800; the webapp proxies it at
  `/worldstate`) — ground truth `{t, wall, pose, joints}` pushed after
  every physics slice (~75Hz), latest-wins per client. The 3D view consumes
  this; future challenge UIs/graders are just more clients.

Physics steps against the wall clock in <=25ms slices (a stall replays as
several smooth slices, never one teleport), with all GL work on the main
thread — macOS GL is main-thread-sensitive. The world always runs on the
host, started by the launcher via `uv` (which is why `uv` is a
prerequisite): native/WSLg GL renders ~7x faster than software GL in
Docker, and physics never competes with the ROS stack for the container's
CPU. The container ships no MuJoCo at all — the driver node is a pure RPC
client. `./innate-sim logs world-server` shows the host server log.

### node.py — mars_sim_driver (the digital twin's hardware)

The ROS adapter that makes the world *be* the robot: a thin RPC client of
the world server publishing the exact topic surface of the hardware
drivers — same topics, types, rates and frame names — so everything above
(Nav2, AMCL, brain, webapp, Foxglove) runs unmodified. The full
topic/service surface with rates is in the `node.py` module docstring;
highlights:

- `/odom` + TF @30Hz, `/scan` @6Hz, cameras @7.5/5Hz JPEG, depth + point
  cloud @8Hz with the real stereo pipeline's [0.25, 2.0]m clamp
- arm/head command topics and `goto_js*` services; streamed setpoints
  replay on the stream's own timeline, so clumped delivery under load
  still plays back at the commanded rate
- latched `/robot_info` `{"simulated": true}` — how the webapp knows to
  render the Three.js view instead of opening WebRTC
- `/virtual_mars/reset` (sim-only)

Camera topics render lazily — no subscribers, no render requests, no GL
work — which is what makes headless runs cheap.

`sim_driver.launch.py` also starts `robot_state_publisher` (same URDF as the
real bringup) and `grid_localizer_sim`, the stand-in for the CUDA-only
grid_localizer: identical lifecycle/service contract, but it seeds AMCL from
ground truth (republishing until AMCL confirms with `/amcl_pose`).

### Assets

The generated geometry (driver meshes in `sim/assets/`, the viewer's hulls,
GLB, robot meshes, and the prebuilt viewer bundle) is not in git:
`./innate-sim up` (or `./innate-sim assets`) downloads the bundle pinned by
`sim/sim-assets.lock` from a GitHub release and extracts it in place
(one-time, ~100 MB). To change the geometry, run the pipeline in
`sim/tools/` (see [`sandbox/README.md`](sandbox/README.md)), then
`uv run tools/publish_assets.py --publish` to publish + repin the lock.
Publishing rebuilds the viewer bundle first, so Node.js is only needed when
editing `sim/viewer` or publishing — never to run the sim. While iterating
on `sim/viewer`, set `INNATE_SIM_VIEWER_DEV=1` so `up` rebuilds the bundle
from source when it is stale; without it the prebuilt bundle is always used
as-is.

### Credits

The apartment environment is derived from ["Appartement"](https://sketchfab.com/3d-models/appartement-6a7a5fe208344b2e8123a88923dbd5b3) by [SrMonteiro](https://sketchfab.com/crispimrafael), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Changes were made: split per room, convex-decomposed for collision, re-exported for rendering (GLB/MuJoCo meshes), and rasterized into a navigation map.
