<!-- markdownlint-disable MD033 MD046 -->
<div align="center">

<p>
  <img src="docs/assets/readme/innate-os-repo-intro.png" alt="Innate OS" width="80%">
</p>

**The lightweight agentic operating system for general-purpose robots**

[![Discord](https://img.shields.io/badge/Discord-Join%20our%20community-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/innate)
[![Documentation](https://img.shields.io/badge/Docs-Read%20the%20docs-blue?style=for-the-badge&logo=readthedocs&logoColor=white)](https://docs.innate.bot)
[![Website](https://img.shields.io/badge/Website-Visit%20us-orange?style=for-the-badge&logo=safari&logoColor=white)](https://innate.bot)
[![ROS 2](https://img.shields.io/badge/ROS%202-Humble-22314E?style=for-the-badge&logo=ros&logoColor=white)](https://docs.ros.org/en/humble/)

</div>

<div align="center">

<img src="docs/assets/readme/mars-compatible.png" alt="MARS robot, the first robot compatible with Innate OS" width="250px"><br>
<sub><strong>MARS</strong>, a small agentic robot for your home.</sub>

</div>

Start with [skills](#skills), [agents](#agents), [additional inputs](#additional-inputs), the [simulator](#simulator), or the [ROS reference](#ros-reference).

> [!TIP]
> Don't have a robot? You can still experiment with the simulator!

<table>
  <tr>
    <td width="37%" align="center" valign="top">
      <img src="docs/assets/readme/screenshot-webapp-card.png" alt="Innate web app control interface" height="300"><br>
      <sub>Web app</sub>
    </td>
    <td width="26%" align="center" valign="top">
      <img src="docs/assets/readme/screenshot-mobile-card.png" alt="Innate mobile app running an agent" height="300"><br>
      <sub>Mobile app</sub>
    </td>
    <td width="37%" align="center" valign="top">
      <img src="docs/assets/readme/screenshot-simulator-card.png" alt="Innate simulator interface" height="300"><br>
      <sub>Simulator</sub>
    </td>
  </tr>
</table>

_Innate OS is developed for MARS; if you want to port it to your robot, we are happy to feature it._

---

## Table of Contents

- [Control](#control)
- [Skills](#skills)
- [Agents](#agents)
- [Simulator](#simulator)
- [Foxglove](#foxglove)
- [Additional Inputs](#additional-inputs)
- [ROS Reference](#ros-reference)
- [More Docs](#more-docs)
- [Contribute](#contribute)

---

## Control

### Web app

With the Innate web app, you can control the robot in real time. Use the virtual joystick controls to drive the base, move the arm, and trigger skills manually.

<img src="docs/assets/readme/screenshot-webapp-card.png" alt="Innate web app" height="300">

It is available at `https://<robot-address>` which can either be its IP or hostname.

### Mobile app

The Innate mobile app is available on both iOS and Android. It allows you to control the robot in real time, just like the web app, but with a more convenient interface.

<table>
  <tr>
    <td width="45%" align="center" valign="top">
      <img src="docs/assets/readme/screenshot-mobile-card.png" alt="Innate mobile app" height="340"><br>
      <sub>Innate Controller app</sub>
    </td>
    <td width="55%" valign="middle">
      <strong>Download the Innate Controller app</strong><br>
      Connect to MARS, drive the robot, run agents and skills, record training data, and manage maps from your phone.<br><br>
      <a href="https://cdn.innate.bot/innate-app-latest-1.3.0.apk"><strong>Android APK (1.3.0)</strong></a><br>
      <sub>Direct APK download.</sub><br><br>
      <a href="https://testflight.apple.com/join/YeChe4A7"><strong>iOS TestFlight</strong></a><br>
      <sub>Join the iOS beta.</sub><br><br>
      <a href="https://docs.innate.bot/robots/innate-controller-app">Controller app docs</a>
    </td>
  </tr>
</table>

---

## Skills

Skills are the core unit of action on Innate robots.

A skill can be digital, like calling a tool, a service or another agent; or physical, like navigating, waving, grasping, recording a demonstration, or executing a learned manipulation policy.

<p align="center">
  <img src="docs/assets/readme/skills-chess-door-opening.gif" alt="Two standalone physical skills: moving a chess piece, then opening a door" width="520"><br>
  <sub>Two standalone skill examples, shown sequentially: moving a chess piece, then opening a door.</sub>
</p>

- **Execute manually** — Run skills from the `innate` CLI.
- **Operate from apps** — Trigger skills through the web app or Innate mobile apps.
- **Run autonomously** — Let agents select and interrupt skills as the world changes.

### Running a skill

On the robot, skills can be inspected and called through the CLI:

```bash
innate skill type innate-os/arm_zero_position
innate skill run innate-os/arm_zero_position @duration=3
```

Custom skills use the same `@name=value` input syntax.

### Trained skills

Some physical skills can be learned from demonstrations.

- Record episodes from the phone app or web app.
- Train a policy with one of the models available on Innate Cloud or locally.
- Deploy the trained model as a skill.

Start here: [Training overview](https://docs.innate.bot/training/overview). To ship a trained model back to the robot, see [Deploy a trained skill](https://docs.innate.bot/training/deploy-trained-skill).

You will find skills in two different directories:

- **Built-in skills** — Located in `workspace/innate_skills/`.
- **Your custom skills** — Stored in `workspace/custom_skills/`. Gitignored and yours to play with.
- **Skill packs** — Any other folder dropped into `workspace/` loads as its own package (ids `<folder>/<name>`). A pack that lives elsewhere on disk is symlinked in (`ln -s /opt/team/skills workspace/team_skills`) and works the same, hot reload included.

Helpers work like normal Python: any `.py` in your skills folder that doesn't define a `Skill` is just a module — `import` it, use relative imports inside subfolders, share across packages by bare name (`from innate_skills import arm_utils`). Device helpers are methods on the interfaces (`self.manipulation.move_to(...)`, `self.mobility.rotate_by(...)`); camera math and Gemini live under `innate` (`from innate import geometry, vision, gemini`).

### Skill definition

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>Replay skill</strong> — replay a recorded motion file.<br>
      Saved as <code>workspace/custom_skills/greet/metadata.json</code>:
      <pre lang="json">{
    "name": "greet",
    "type": "replay",
    "guidelines": "Greet the user with a friendly arm wave.",
    "inputs": {},
    "wheeled": false,
    "downloads": {
        "episode_0.h5": "https://your-cdn.com/greet/episode_0.h5"
    },
    "execution": {
        "model_type": "replay",
        "replay_file": "episode_0.h5",
        "replay_frequency": 50.0,
        "start_pose": [1.57693225, -0.6, 1.4772235, -0.73784476, 0.0, 0.0],
        "end_pose": [1.57693225, -0.6, 1.4772235, -0.73784476, 0.0, 0.0]
    }
}</pre>
    </td>
    <td width="50%" valign="top">
      <strong>Code skill</strong> — call the mobility interface to move forward.<br>
      Saved as <code>workspace/custom_skills/move_forward.py</code>:
      <pre lang="python">from innate import Mobility, Skill, SkillReturn


class MoveForward(Skill):
    """Move the robot forward by a given distance in meters."""

    mobility: Mobility          # declare what you use; the runtime injects it

    def execute(self, distance_m: float = 0.5) -> SkillReturn:
        speed = 0.2  # m/s
        duration = distance_m / speed
        self.mobility.send_cmd_vel(linear_x=speed, duration=duration)
        self.sleep(duration)    # like time.sleep, but a Stop unwinds it
        return f"Moved forward {distance_m} m"
</pre>
      The return value is the run's result message; call
      <code>self.fail(message)</code> to end the run as a failure.
      Cancellation is the framework's job: <code>self.sleep</code> (and every
      blocking framework call) raises the moment a Stop lands, the base is
      braked automatically, and the run reports CANCELLED — skills carry no
      cancel code.
    </td>
  </tr>
</table>

---

## Agents

Agents allow Innate robots to run autonomously following your instructions.

They make the robot think in a high-frequency loop using a multimodal model, for example a VLM that is constantly observing the world.

An agent consists of:

- A **set of skills** the robot is allowed to use
- A **system prompt** that defines the robot's behavior
- An **agent loop** that connects the model to observations, memory, tools, and robot actions

<p align="center">
  <img src="docs/assets/readme/agent-clean-room.gif" alt="Pick up and put away skills chained in an agent to clean a room" width="520"><br>
  <sub>Pick up and put away skills chained in an agent to clean a room.</sub>
</p>

### Specificities of multimodal agents

Multimodal agents have different constraints than purely digital agents: they need to **observe continuously**, run at a **high frequency** to react, and to be able to **interrupt** a running skill when the world has changed.

### Agent definitions

You can find agents in two different directories:

- **[`workspace/innate_agents/`](workspace/innate_agents/)** — Built-in agents shipped with Innate OS.
- **[`workspace/custom_agents/`](workspace/custom_agents/)** — Your local agents. Gitignored and yours to play with.

Here is an example of a simple agent to navigate:

A minimal agent file, saved as `workspace/custom_agents/navigate_agent.py`:

```python
from brain_client.agents.types import Agent


class NavigateAgent(Agent):
    """An agent that can navigate to requested positions."""

    @property
    def id(self):
        return "navigate_agent"

    @property
    def display_name(self):
        return "Navigate"

    def get_skills(self):
        return ["innate-os/navigate_to_position"]

    def get_inputs(self):
        return ["micro"]

    def get_prompt(self):
        return "You are a helpful robot. When asked, navigate to the requested location using the navigate_to_position skill."

    def uses_map(self):
        return True
```

Override `uses_map()` to include the latest navigation occupancy map in every
local-brain turn. The brain reads the latched `nav_msgs/OccupancyGrid` on
`/map`, renders it as a labeled JPEG, and keeps only the current map in model
history. If no valid map is available (including map-free mode's placeholder),
the turn continues without a map image. Agents that do not override
`uses_map()` keep the default `False` behavior.

### Testing agents in sim

Use the [simulator](#simulator) to test custom agents before running them on a physical robot.

---

## Simulator

Innate OS includes a MuJoCo digital twin of MARS that runs the **real robot software** -- the same navigation stack, skills, brain client, and webapp as the physical robot, with only the hardware drivers swapped for a simulated equivalent. Use it to build and test skills, agents, and input devices before you have a robot on your desk.

<p align="center">
  <img src="docs/assets/readme/sim.png" alt="Driving the simulated MARS robot through its apartment in the browser" width="85%">
</p>

```bash
./innate-sim setup
./innate-sim up
```

The first `up` provisions everything automatically (simulation assets, the 3D viewer bundle, the Docker image, the ROS workspace build); later runs reuse all of it and start in seconds. The terminal opens a live dashboard, and the robot webapp at [https://localhost](https://localhost) is the sim UI -- drive and operate the simulated MARS exactly like a real one, with a live 3D view instead of camera streams.

```bash
./innate-sim status         # show current runtime state
./innate-sim sh             # open a shell inside the ROS container
./innate-sim logs os-session # inspect runtime logs (see `logs --help` for targets)
./innate-sim down           # stop the runtime
```

See [`sim/README.md`](sim/README.md) for everything else: the day-to-day workflow, the ROS-free VirtualMars Python API (with a walkthrough notebook), and the architecture.

---

## Foxglove

[Foxglove Studio](https://foxglove.dev) gives you a live view of TF, `/scan`, camera images, point clouds, and `/cmd_vel` teleop for debugging. In the [simulator](#simulator) the bridge is always on; on a physical robot it is opt-in:

```bash
innate foxglove start   # start the bridge (ws://<robot-ip>:8765)
innate foxglove stop    # stop it
innate foxglove         # status
```

Then connect Foxglove Studio to the printed `ws://` URL.

> **Over Wi-Fi, subscribe to the `/mars/main_camera/remote/*` topics, not the raw ones.** The raw camera images (~0.9 MB/frame) and point clouds (`/mars/main_camera/points`, ~10 MB/s) are far more than a Wi-Fi link can carry, so every panel — including `/cmd_vel` — falls seconds behind. The `remote/` namespace carries the same topics throttled to ~2 Hz (and images already compressed), which fits comfortably. Prefer `.../compressed` image topics and mono `remote/points` over `remote/points_color`.

---

## Additional Inputs

Innate OS provides an SDK for streaming new data into running agents. Innate robots are designed to be naturally expandable: add a new sensor, expose it as an input device, and let agents request it by name.

Input devices live in [`workspace/inputs/`](workspace/inputs/) and are pure Python. They should not import ROS directly.

<details>
<summary>Thermometer input example</summary>

```python
# workspace/inputs/thermometer_input.py

import threading
import time

from brain_client.inputs.types import InputDevice


def read_thermometer_celsius() -> float:
    # Replace this with your hardware, websocket, serial, or API read.
    return 21.5


class ThermometerInput(InputDevice):
    def __init__(self, logger=None):
        super().__init__(logger)
        self._stop_event = threading.Event()
        self._thread = None

    @property
    def name(self) -> str:
        return "thermometer"

    def on_open(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def on_close(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _loop(self):
        while not self._stop_event.is_set():
            self.send_data(
                {"celsius": read_thermometer_celsius(), "timestamp": time.time()},
                data_type="custom",
            )
            time.sleep(1.0)
```

An agent or directive can then request the input by name:

```python
def get_inputs(self):
    return ["thermometer"]
```

</details>

See [`docs/INPUT_DEVICES.md`](docs/INPUT_DEVICES.md) for the full input-device lifecycle.

---

## ROS Reference

Innate OS is currently based on ROS 2, the reference framework for robotics operating systems. Most builders should start with skills, agents, inputs, and the simulator. Changing the core OS is not recommended for normal usage, but it is possible.

<table>
  <tr>
    <td width="20%" valign="top"><strong><a href="ros2_ws/">ros2_ws/</a></strong><br>Robot runtime workspace.</td>
    <td width="20%" valign="top"><strong><a href="docs/SYSTEM_OVERVIEW.md">System Overview</a></strong><br>Architecture reference.</td>
    <td width="20%" valign="top"><strong><a href="scripts/launch_ros_in_tmux.sh">Startup</a></strong><br>Robot node wiring.</td>
    <td width="20%" valign="top"><strong><a href="scripts/update/README.md">Updates</a></strong><br>Services and CLI commands.</td>
    <td width="20%" valign="top"><strong><a href="config/">config/</a></strong><br>DDS, systemd, udev, audio, Bluetooth, sounds, and shell setup.</td>
  </tr>
</table>

<details>
<summary>Main ROS 2 runtime packages</summary>

- **[mars_control](ros2_ws/src/mars_bot/mars_control)** - top-level robot app node, rosbridge websocket server for the mobile/web app, and low-latency UDP receiver for leader-arm teleop.
- **[mars_bringup](ros2_ws/src/mars_bot/mars_bringup)** - hardware bringup for motors, base, IMU, and LiDAR, plus `robot_state_publisher` for the TF tree.
- **[mars_arm](ros2_ws/src/mars_bot/mars_arm)** - arm and head servo driver and KDL-based IK solver.
- **[mars_cam](ros2_ws/src/mars_bot/mars_cam)** - stereo main camera, arm camera, VPI stereo depth estimator, WebRTC streamer, and stereo calibration action server.
- **[mars_nav](ros2_ws/src/mars_bot/mars_nav)** - Nav2-based navigation, SLAM mapping, and the mode manager that switches between `mapfree`, `mapping`, and `navigation`.
- **[brain_client](ros2_ws/src/brain/brain_client)** - bridge to the Innate cloud brain, websocket client, skills action server, and user input manager.
- **[manipulation](ros2_ws/src/brain/manipulation)** - records and replays manipulation demonstrations and runs learned or scripted manipulation policies.
- **[innate_logger](ros2_ws/src/cloud/innate_logger)** - uploads robot logs and telemetry to the Innate cloud.
- **[innate_training_node](ros2_ws/src/cloud/innate_training_node)** - collects training episodes and pushes them to the training cloud.
- **[innate_uninavid](ros2_ws/src/cloud/innate_uninavid)** - UniNaVid vision-language navigation client.

</details>

---

## More Docs

- [Innate documentation](https://docs.innate.bot) - canonical docs for setup, simulator, skills, agents, training, and robot operation.

---

## Contribute

We welcome contributions to Innate OS and will be happy to feature applications written on top of it here–and robots using it.

A huge thanks to all people in the community who helped by contributing, providing feedback, and building on Innate OS.

If you want to help, feel free to reach out on [Discord](https://discord.gg/innate).
