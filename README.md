<!-- markdownlint-disable MD033 MD046 -->
<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme/innate-os-repo-intro-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/readme/innate-os-repo-intro.png">
  <img src="docs/assets/readme/innate-os-repo-intro.png" alt="Innate OS" width="70%">
</picture>

**The lightweight agentic operating system for general-purpose robots**

[![Discord](https://img.shields.io/badge/Discord-Join%20our%20community-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/innate)
[![Documentation](https://img.shields.io/badge/Docs-Read%20the%20docs-blue?style=for-the-badge&logo=readthedocs&logoColor=white)](https://docs.innate.bot)
[![Website](https://img.shields.io/badge/Website-Visit%20us-orange?style=for-the-badge&logo=safari&logoColor=white)](https://innate.bot)
[![ROS 2](https://img.shields.io/badge/ROS%202-Humble-22314E?style=for-the-badge&logo=ros&logoColor=white)](https://docs.ros.org/en/humble/)

<img src="docs/assets/readme/mars-compatible.png" alt="MARS, a small agentic robot for your home" width="250px">

<sub><strong>MARS</strong> is a small agentic robot for your home. Innate OS is the runtime for its skills, agents, inputs, simulation, and control.</sub>

</div>

## Try it without a robot

<p align="center">
  <a href="https://sim.innate.bot"><img src="docs/assets/readme/sim.png" alt="Driving the simulated MARS robot in the browser" width="85%"></a>
</p>

**[Try the live simulator →](https://sim.innate.bot)** — no install, no robot.

Or run the same stack locally. One command on macOS, Linux, and WSL2:

```bash
curl -fsSL https://link.innate.bot/sim | sh

cd innate-os
./innate-sim up
```

The simulator runs the software that ships on MARS, with simulated hardware in place of the physical drivers. Open [https://localhost](https://localhost) to drive it, run skills, and talk to the agent.

```bash
./innate-sim status
./innate-sim sh
./innate-sim logs startup
./innate-sim down
```

On a physical robot the web app is at `https://<robot-address>`. The same robot is on your phone via the [Android APK](https://cdn.innate.bot/innate-app-latest-1.4.0.apk) or [iOS TestFlight](https://testflight.apple.com/join/YeChe4A7).

See the [simulator docs](https://docs.innate.bot/simulator) or [`sim/README.md`](sim/README.md) for the full workflow.

## Skills

Skills are the unit of action on an Innate robot — a software call, a motion, or a learned policy.

<p align="center">
  <img src="docs/assets/readme/skills-chess-door-opening.gif" alt="Two standalone skills: moving a chess piece, then opening a door" width="520">
</p>

Run them from the [web app](https://docs.innate.bot/robots/web-app), the [phone app](https://docs.innate.bot/robots/innate-controller-app), or an agent. Write your own in `workspace/custom_skills/` — the [workspace guide](workspace/README.md) is the hello world.

```python
from innate import Mobility, Skill, SkillReturn


class MoveForward(Skill):
    """Move the robot forward by a given distance."""

    mobility: Mobility

    def execute(self, distance_m: float = 0.5) -> SkillReturn:
        speed = 0.2
        duration = distance_m / speed
        self.mobility.send_cmd_vel(linear_x=speed, duration=duration)
        self.sleep(duration)
        return f"Moved forward {distance_m} m"
```

Built-in skills live in `workspace/innate_skills/`. You can also [record a motion and train a policy](https://docs.innate.bot/training/overview), then deploy it as a skill.

**[Skills documentation →](https://docs.innate.bot/software/skills)**

## Agents

Agents let the robot act on its own. One combines a set of skills, a prompt, inputs, and a loop that turns observations into actions.

<p align="center">
  <img src="docs/assets/readme/agent-clean-room.gif" alt="An agent chaining pick-up and put-away skills to clean a room" width="520">
</p>

```python
from innate_skills.navigate_to_position import NavigateToPosition
from inputs.micro_input import MicroInput

from innate import Agent, InputRef, SkillRef


class NavigateAgent(Agent):
    @property
    def id(self) -> str:
        return "navigate_agent"

    @property
    def display_name(self) -> str:
        return "Navigate"

    def get_skills(self) -> list[SkillRef]:
        return [NavigateToPosition]

    def get_inputs(self) -> list[InputRef]:
        return [MicroInput]

    def get_prompt(self) -> str:
        return (
            "You are a helpful robot. When asked, navigate "
            "to the requested location."
        )
```

List skills and inputs as the classes themselves, so your editor catches a typo before the robot does.

Save it in `workspace/custom_agents/`. Because the robot lives in the physical world, agents observe continuously and can interrupt a running skill when the world changes.

**[Agents documentation →](https://docs.innate.bot/software/agents)**

## Inputs

Stream new data into a running agent — a custom sensor, a webhook, an API. Devices live in `workspace/inputs/` and are requested by class:

```python
from inputs.arm_vitals_input import ArmVitalsInput

from innate import InputRef


def get_inputs(self) -> list[InputRef]:
    return [ArmVitalsInput]
```

**[Input devices →](https://docs.innate.bot/software/inputs)**

## Documentation

This README is an introduction. The rest lives in the docs:

- [Building with the simulator](https://docs.innate.bot/simulator/building-with-the-simulator)
- [Workspace guide](workspace/README.md)
- [Getting started](https://docs.innate.bot/get-started/mars-quick-start)
- [Simulator](https://docs.innate.bot/simulator)
- [Skills](https://docs.innate.bot/software/skills)
- [Agents](https://docs.innate.bot/software/agents)
- [Input devices](https://docs.innate.bot/software/inputs)
- [Training](https://docs.innate.bot/training/overview)
- [Web app](https://docs.innate.bot/robots/web-app) and [controller app](https://docs.innate.bot/robots/innate-controller-app)
- [CLI](https://docs.innate.bot/software/innate-cli)
- [ROS 2 core and system overview](https://docs.innate.bot/software/ros2-core)

Most builders should start with skills, agents, inputs, and the simulator. Changing the ROS core is possible; it is not the usual path.

## Contributing

Innate OS is open source under the [Apache 2.0](LICENSE) license. We welcome contributions, apps built on it, and ports to other robots — we will be happy to feature them.

**[Contributing guide](.github/CONTRIBUTING.md)** · **[Discord](https://discord.gg/innate)**
