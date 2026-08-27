# The Innate Simulator

<p align="center">
  <img src="../docs/assets/readme/sim.png" alt="Driving the simulated MARS robot through its apartment in the browser" width="85%">
</p>

A complete simulation of the [MARS](https://innate.bot) home robot that runs
on your laptop — **no robot required**. It is a true digital twin: the same
software that runs on a physical MARS (navigation, skills, the AI agent, the
web app) runs here against a physics simulation of the robot in a furnished
apartment. You drive it, run skills on it, and talk to its agent from your
browser, exactly as you would with the real thing — which also means
anything you build against the simulator works unchanged on a real MARS.

## Setup

```bash
curl -fsSL https://link.innate.bot/sim | sh
```

That is the whole install on Linux and WSL2: it asks before installing
anything missing (Docker, uv, git, the rendering libraries), clones this
repository into `innate-os/` in the directory you run it from, asks how the
agent should reach a cloud LLM, and downloads the runtime — so the first
`./innate-sim up` is a start, not a download.

**On macOS, install [Docker
Desktop](https://docs.docker.com/desktop/install/mac-install/) first** and open
it once; the installer handles everything after that. Then:

```bash
cd innate-os && ./innate-sim up
```

Set `INNATE_DIR` to put the checkout somewhere else.

**Already cloned this repository?** Run the same script from inside it — it
skips the clone and sets up the checkout you have:

```bash
sh scripts/install-sim.sh
```

`./innate-sim setup` does the same *last* step on its own, but installs no
prerequisites: use it when Docker and uv are already in place.

Prefer to do it yourself? The rest of this section is the same setup by hand.

You need two tools installed; everything else (world geometry, the Docker
image, the ROS build) is provisioned automatically on first start:

- **Docker** — runs the robot's software stack in a container.
- **uv** — runs the physics world natively on your machine
  (`./innate-sim setup` offers to install this one for you).

16 GB of RAM and 4 or more CPU cores is comfortable; 8 GB runs it, but the
physics and the robot's software compete for the machine. The first start
downloads a few GB, so it takes a while; later starts take seconds.

<details>
<summary><b>macOS</b></summary>

Install [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/)
and open it once to accept its terms. macOS has no headless Docker: containers
are Linux processes, so they run inside the VM Docker Desktop provides.

The installer does not do this one for you — a desktop app with a licence to
agree to and a first launch to sit through is not something a shell script can
finish — so it checks, and says so if it is missing.

</details>

<details>
<summary><b>Linux (Ubuntu / Debian / Raspberry Pi OS)</b></summary>

Use Docker's own install script — it sets up the official apt repo and
installs Docker the same way on every Debian-family distro:

```bash
curl -fsSL https://get.docker.com | sudo sh
```

Then let your user talk to Docker:

```bash
sudo usermod -aG docker $USER
```

Install the rendering libraries. On a headless server or VM these provide
offscreen rendering; on a desktop with working OpenGL they're mostly already
present, and the extra software-rendering library is harmless — so it's safe
to run either way:

```bash
sudo apt install libegl1 libgl1 libopengl0 libosmesa6
```

</details>

<details>
<summary><b>Windows (WSL2)</b></summary>

The sim runs inside WSL2. In PowerShell:

```powershell
wsl --install -d Ubuntu
```

then open the Ubuntu terminal and follow the **Linux** steps above. WSLg
(included in current WSL) gives the sim GPU-accelerated rendering
automatically.

Install into your Linux home folder — run `cd ~` first if the prompt shows a
path starting with `/mnt/`. That prefix is the Windows drive seen from Linux,
and every file the container reads there crosses a bridge slow enough to turn
a two-minute start into a coffee break.

One caution: use exactly **one** Docker — either Docker Desktop (with WSL
integration enabled) or `docker.io` installed inside WSL. Having both
installed makes the `docker` command hang in confusing ways.

</details>

Then, from the repository root:

```bash
./innate-sim setup     # prerequisites + agent keys, then downloads the runtime
./innate-sim up        # starts everything; leave the live dashboard open
```

`setup` downloads the images, the world geometry and the sim world's Python
environment once it has your keys, which is the slow part; `up` then starts in
a minute or two rather than after a multi-gigabyte download. `--no-prefetch`
skips it and leaves the downloading to the first `up`.

`setup` asks how the robot's AI agent reaches a cloud LLM. The agent loop
itself always runs on the robot (`brain_client`); the choice is only about
which key it thinks with:

- **Your own Gemini key** — the choice for most people. Create a
  [Gemini API key](https://aistudio.google.com/api-keys) and the agent calls
  Google directly with it. Everything works except voice: the web app's speak
  bar is disabled without a service key.
- **Innate service key** — pick this if you already have one: it ships with a
  MARS robot, and we hand them out to people building on the simulator. The
  agent calls Gemini through Innate's proxy and the robot speaks. Ask on
  [Discord](https://discord.gg/innate) if you would like one.
- **None** — no agent; you can still drive, navigate, and trigger skills
  manually.

Rerun `./innate-sim setup` anytime to switch.

**Open [https://localhost](https://localhost)** (accept the self-signed
certificate) — you are looking at the robot's web app. Drive with the
joystick or WASD, switch between the 3D view, the robot's cameras, and the
map, trigger skills, and chat with the agent. Add `?simperf` to the URL for
a frame-time / latency HUD.

Every error along the way is written to tell you exactly what to do next.
If something stops you anyway, we want to hear about it —
[join our Discord](https://discord.gg/innate).

### Everyday commands

```bash
./innate-sim status      # startup checks + health snapshot
./innate-sim logs startup   # startup logs; `logs brain` for the running stack
./innate-sim sh          # shell into the container; `innate build` rebuilds ros2_ws
./innate-sim down        # stop the container + world server (keeps data)
./innate-sim clean       # remove containers/volumes (keeps .env + config)
```

## Build skills and agents

The simulator shares the repository's [`workspace/`](../workspace/) with the
container, so the skills and agents you write for the sim are the ones a
real robot runs — develop here, deploy there. Start with the docs:

- [Skills](https://docs.innate.bot/software/skills) — teach the robot new
  abilities in Python (or by demonstration on a real robot).
- [Agents](https://docs.innate.bot/software/agents) — give the AI brain
  goals, personality, and access to your skills.

### Challenges

Scored tasks for what you build: find a collapsed person and stay with them,
push a soccer ball to the dog, collect food orders from residents, or celebrate
good news with the skill you wrote.
Pick one from the panel at the top-left of the Agent page — it resets the
world, drops the props the scenario needs, and ticks a goal checklist with a
timer. Results (passed, attempts, best time) persist in
`workspace/challenges.json`.

The **world server** judges, not the robot: goals are read from MuJoCo's own
state, and the robot stack is never told a challenge exists. Passing means
the robot really did the thing, not that it reported doing it.

A challenge defines the **environment and judge**, not the agent being tested.
It may place props, create conversational residents, keep scenario facts
hidden, and score resulting events; it does not install a directive or add
robot skills. Select or build the agent separately, then run it against the
challenge.

A challenge is a sidecar in [`sim/challenges/`](challenges/) — the same shape
as a prop's — exporting `CHALLENGE = Challenge(...)`:

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
)
```

`setup` drops props by name (the sidecars in [`sim/props/`](props/)). Goals
are judged strictly in order and latch once true. The predicates are `Near`,
`InCircle`, `InRect`, `Hold` (a dwell — true for N seconds without a break),
`SkillDone` (optionally guarded by another predicate, so "wave WHILE next to
the person" counts and a wave across the room does not), `EventSeen`, and
`AllOf`/`AnyOf`.

Simple challenges are one-file sidecars. A challenge with private stateful
environment behavior may instead be a package whose `__init__.py` exports the
same `CHALLENGE` value and attaches a `ChallengeRuntime`. The runtime receives
world snapshots and external events, then returns trusted environment events
and ordinary chat replies. Scenario concepts and private facts stay in that
package rather than entering the generic challenge engine; see
`sim/challenges/40_household_orders/`.

Everything but robot lifecycle and speech events is read from the world. Those
events arrive over rosbridge, so with rosbridge down the world-state goals
still work while skill and resident-dialogue goals simply never fire.

### When do changes take effect?

| You edited | What to do |
|---|---|
| skills or agents in `workspace/` | **nothing** — they hot-reload on save (fallback: `ros2 service call /brain/reload std_srvs/srv/Trigger` in the container) |
| parameters in `config/` | inside the container: `innate restart` |
| ROS code in `ros2_ws/src/` | inside the container: `innate build` (it stops the nodes, builds, and restarts them) |
| the simulated world (`mars_sim_driver/` world/server) | `./innate-sim down && ./innate-sim up` — this part runs on the host |
| challenge or prop sidecars (`sim/challenges/`, `sim/props/`) | same restart — they are loaded by the host world server at startup |
| launcher / webapp files | just rerun `./innate-sim up` / reload the browser |

The container's ROS session lives in tmux (`./innate-sim sh`, then
`tmux attach -t innate`): one window per subsystem (zenoh, rosbridge-app,
sim-driver, nav-brain, behavior, arm-ik, vision-nav, console-webapp,
foxglove).

---

## Advanced

### VirtualMars: the sim as a Python object

For scripts, notebooks, and RL loops there is a second way in that needs no
ROS and no Docker: the apartment, the robot, its cameras, lidar and arm as a
single Python object — instantiate several in one process for parallel
rollouts.

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
sim.step(1.0)  # settle from spawn; step(dt) runs physics
sim.set_cmd_vel(0.3, 0.5)  # vx m/s, wz rad/s (0.5s watchdog)
sim.set_joint_target("joint2", -1.0)  # arm/head PD servo setpoints
x, y, yaw = sim.pose()  # ground truth
rgb = sim.render_rgb("main")  # 640x480 ("wrist" = arm camera)
depth = sim.render_depth("main")  # meters; robot's own geoms excluded
scan = sim.lidar_scan(360, 12.0)  # planar lidar off the visual surfaces
grid, ox, oy = sim.occupancy_grid()  # rasterized nav map (-1/0/100)
sim.reset()  # back to spawn, arm home
```

Rendering is lazy — pure physics is cheap. For a native MuJoCo viewer window
(WASD driving, arm sliders in a browser control panel):

```bash
cd sim && uv run sandbox/drive_mars.py
```

More dev tooling (physics stress gate, asset pipeline) is documented in
[`sandbox/README.md`](sandbox/README.md).

### ROS access

[Foxglove Studio](https://foxglove.dev) is a free app for visualizing what the
robot sees — camera images, the map and laser scan, coordinate frames, and the
`/cmd_vel` velocity commands from teleop. It's the easiest way to look inside the
running sim.

You don't need to start anything: the sim launches a Foxglove bridge for you.
To connect:

1. Install and open [Foxglove Studio](https://foxglove.dev/download) (desktop or web).
2. Choose **Open connection → Foxglove WebSocket**.
3. Enter `ws://localhost:8765` and connect.
4. Add panels (e.g. Image, 3D, Raw Messages) and pick the topics you want to see.

> **Which port?** The sim's startup log always prints the exact address to use.
> To force a specific port or network interface, set `SIM_FOXGLOVE_PORT` /
> `SIM_FOXGLOVE_BIND` before starting the sim.

Because the sim runs on your own machine, every topic is fast — feel free to view
the full-resolution cameras and point clouds. This is different from a **physical
robot on Wi-Fi**, where those topics are too large to stream smoothly; there you'd
use the lighter `/mars/main_camera/remote/*` topics instead. See
[Foxglove](../README.md#foxglove) in the main README for that case.

### Working on the robot code

`./innate-sim sh` drops you into the container, where the same `innate` CLI
as on a real robot manages the ROS stack. Your checkout is bind-mounted into
it (`ros2_ws/`, `workspace/`, `scripts/`, `config/`), so files you edit on
the host are immediately visible inside — you only choose how to reload them:

```bash
innate                   # status: mode, node health, command hints
innate view              # attach the tmux session running the stack (Ctrl-b d to detach)
innate restart           # stop + relaunch all ROS nodes (no rebuild)
innate build             # stop nodes, colcon-build ros2_ws, restart
innate build <pkg…>      # same, but only the named packages (faster)
innate build release     # optimized build (CMAKE_BUILD_TYPE=Release)
innate skill list        # skills the brain currently offers
innate skill run <id> @param=value   # trigger a skill from the shell
```

[When do changes take effect?](#when-do-changes-take-effect) above has the
answer for each kind of edit. `ros2_ws/` is the one worth knowing by heart:
`colcon` installs a *copy*, so editing a node and restarting is not enough —
`innate build mars_nav` (naming the package keeps the cycle short) rebuilds
and restarts in one step.

The trap is the simulated world itself — `mars_sim_driver`'s `world.py`,
`core.py` and `world_server.py`. That process runs on the **host**, outside
the container, on every platform, importing straight from your checkout.
`innate build` rebuilds the container's copy, but the world server never
loads it. Restart that one from the host:
`./innate-sim down && ./innate-sim up`.

**Several clones at once.** Each checkout gets its own container, network and
named volumes, so a branch clone beside your main one keeps its own compiled
workspace. They still share the host's ports, so only one can be `up` at a
time — starting a second stops with the name and path of the checkout holding
them.

`innate view` is the fastest way to see why a node is unhappy: the tmux
session has one window per subsystem (zenoh, rosbridge-app, sim-driver,
nav-brain, behavior, arm-ik, vision-nav, console-webapp, foxglove), each
showing that process's live output.

Prefer your own ROS tooling? A rosbridge server is also available at
`ws://localhost:9090`.

### Configuration

- repo-root `.env` — secrets (`INNATE_SERVICE_KEY`, brain backend keys);
  `./innate-sim setup` walks through them.
- `config/settings.yaml` — optional non-secret ROS parameter tunables and
  extra agent/skill dirs.
- `sim/config.toml` — optional overrides (OS image, build behavior),
  created from `config.toml.template` by setup.
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

The layer you use directly in the VirtualMars section above.
`update_camera()` / `read_rgb()` are split (same for depth) so callers can
update the scene under a lock but render outside it — that's what keeps
physics from stalling in the world server.

### world_server.py — the world host

Hosts one VirtualMars behind two localhost interfaces, one per kind of
consumer (the invariant: robot software sees the world only through the
robot adapter; humans and tools see it only through the observer stream):

- **driver RPC** (port 8799) — sensing/actuation for node.py, robot-shaped
  and rate-limited like real hardware. Renders are demand-paced: a camera
  or depth product renders once per client pull (~8Hz), never free-running.
- **observer stream** (WebSocket, port 8800; the webapp proxies it at
  `/worldstate`) — ground truth `{t, wall, pose, joints, objects, challenge}`
  pushed after every physics slice (~75Hz), latest-wins per client, and stage
  commands (the viewer's prop chips: `drop_prop_at` releases a prop over a spot
  you picked and lets physics settle it, `place_prop_at_robot` / `place_group`
  set props down at rest at their own reach offsets, `remove_prop` /
  `remove_all_props` send them back off-map, which is where every prop starts;
  plus `start_challenge` / `abort_challenge`) accepted back on the same socket.
  Scenery and scoring, never robot control. Rosters that never change while the
  server runs (the props, the challenges) go out once per connection instead of
  riding every broadcast. The 3D view and the challenge panel are just two
  clients of the same stream.

Physics steps against the wall clock in <=25ms slices (a stall replays as
several smooth slices, never one teleport), with all GL work on the main
thread — macOS GL is main-thread-sensitive. The world always runs on the
host, started by the launcher via `uv` (which is why `uv` is a
prerequisite): native/WSLg GL renders ~7x faster than software GL in
Docker, and physics never competes with the ROS stack for the container's
CPU. The container ships no MuJoCo at all — the driver node is a pure RPC
client. `./innate-sim logs world-server` shows the host server log.

### challenges.py — the judge

Hosted by the world server, which is what makes the verification honest: the
judge reads the same MuJoCo state the physics runs on, and nothing about a
challenge is ever published to the robot stack. A run is a `Challenge`
sidecar (see [Challenges](#challenges)) plus three pieces of engine:

- `tick()` runs on every state broadcast, judges the next unmet goal against
  the world it was handed, and returns the `challenge` block the stream
  carries. It never touches the sim itself — the poses and prop centres are
  gathered under the sim lock by the caller — so any frontend is a renderer,
  never a grader.
- `start()`/`abort()` arrive as observer commands. `start()` deactivates,
  resets the world and drops the scenario's props, and only then publishes
  the run, so a tick landing mid-setup can never judge a half-built scene.
- `SkillEventBridge` subscribes to `/brain/skill_status_update` over the
  stack's rosbridge for `SkillDone`. Entirely best-effort: the sim never
  waits on it and never fails because of it.
- `ChallengeChatBridge` listens to normal robot speech on `/brain/chat_out`
  and publishes runtime-generated environment replies on `/brain/chat_in`.
  Scenario-private facts stay inside the runtime; the robot receives only its
  ordinary chat output.

Distances are measured to a prop's visual centre, not its body origin (a
human scan stands feet-at-origin), which the prop sidecar already knows —
see `PropRegistry.center_xy`. Results land in `workspace/challenges.json`,
written atomically. A broken challenge file is skipped at load; a predicate
that raises fails that run and nothing else.

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

The generated geometry is not in git, and it now reaches you two different ways.

`sim/assets/` (the MuJoCo store the world server reads) is *extracted to disk*:
`./innate-sim up` — or `./innate-sim assets`, which needs no Docker at all —
fetches a single layer of the asset image over plain HTTPS (~85 MB, one-time;
`sim/launcher/oci.py`). The world server runs on the host and writes caches
beside the geometry, so this one has to be a real, writable directory.

The viewer's assets (per-room apartment glbs, robot meshes, collision hulls) are
*never staged on the host*: compose mounts them straight out of the same image
(`type: image`, which needs Compose 2.35+).

The SimSession bundle the webapp loads is mounted the same way, but from a
**separate** image, `innate-os-sim-viewer`. The two are split because their
change rates differ by orders of magnitude: the geometry costs hours to
regenerate and moves a few times a year, the bundle is ~1 MB and moves on every
viewer commit. While they shared one content-addressed tag, a one-line edit in
`sim/viewer/src` renamed the asset image too, and `up` then asked GHCR for a tag
CI had never published. Either way `up` needs no Node.js.

The bundle's tag is `inputs-<hash>` over `sim/viewer` **as it is on disk** —
membership from `git ls-files --cached --others --exclude-standard`, so an
edited file and a brand-new untracked one both count, while gitignored cruft
does not. One hash serves both sides: a clean checkout computes what CI
computed and pulls the published image, and any edit names an image CI cannot
have published, so the launcher builds `sim/viewer/Dockerfile` itself as
`innate-os-sim-viewer-local:inputs-<same hash>`. Same Dockerfile CI uses, so
**no path here needs Node.js on the host** — only Docker, which running the sim
already requires. There is no flag to set: editing the files is the signal.

The asset image's tag is `inputs-<hash>` over the tracked files it is built
from — the pipeline scripts, the Dockerfiles, and the pinned URLs of the raw
geometry. Nothing about it is recorded anywhere: change any of those and the
tag moves, CI builds it once, and every later publish of the same tag is
skipped outright. To change the geometry, edit the pipeline in `sim/tools/`
(see [`sandbox/README.md`](sandbox/README.md)) and push; CI rebuilds it.

### Credits

The apartment environment is derived from ["Appartement"](https://sketchfab.com/3d-models/appartement-6a7a5fe208344b2e8123a88923dbd5b3) by [SrMonteiro](https://sketchfab.com/crispimrafael), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Changes were made: split per room, convex-decomposed for collision, re-exported for rendering (GLB/MuJoCo meshes), and rasterized into a navigation map.
