<!-- markdownlint-disable MD033 MD046 -->
<div align="center">

# Innate OS

**The open-source runtime for autonomous physical agents.**

<img src="docs/assets/readme/agent-clean-room.gif" alt="A physical robot chaining pick-up and put-away skills to tidy a room (sped-up footage)" width="720">

Build autonomous robots from reusable skills, continuous perception, spatial memory, and learned policies. Run your agents in simulation, then on physical hardware.

[![Try in browser](https://img.shields.io/badge/Try_in_browser-401FFB?style=for-the-badge)](https://sim.innate.bot)

**No robot required to get started.**

[Documentation](https://docs.innate.bot) · [Discord](https://discord.gg/innate) · [Website](https://innate.bot)

</div>

## What you can build

<table>
  <tr>
    <th align="center">Agentic task execution</th>
    <th align="center">Spatial memory</th>
  </tr>
  <tr>
    <td align="left">Combine navigation, pick-up, and put-away skills around a goal. The agent observes while skills run and can interrupt them as the world changes. <a href="workspace/innate_agents/demo_agent.py">See the demo agent →</a></td>
    <td align="left">Ask about a place or object the robot has seen. Search remembered views for an image, map coordinates, and when it was seen, then navigate to the match. <a href="workspace/innate_skills/search_memory.py">See memory search →</a></td>
  </tr>
  <tr>
    <th align="center">Learned manipulation</th>
    <th align="center">Simulation → hardware</th>
  </tr>
  <tr>
    <td align="left">Record demonstrations, train a LeRobot policy, and deploy it as a skill alongside Python code and recorded motions. <a href="https://docs.innate.bot/training/overview">Explore training →</a></td>
    <td align="left">Build with the same agents, skills, and web app in the simulator and on MARS. Simulated hardware replaces physical drivers. <a href="https://docs.innate.bot/simulator/building-with-the-simulator">Build in simulation →</a></td>
  </tr>
</table>

## See it in action

<table>
  <tr>
    <td width="50%"><a href="https://docs.innate.bot/software/skills"><img src="docs/assets/readme/skills-chess-door-opening.gif" alt="Physical skills on MARS: moving a chess piece and opening a door" width="420"></a></td>
    <td width="50%"><img src="docs/assets/readme/mars-agent-demo.webp" alt="A real MARS navigating to, picking up, and dropping a blue sock" width="420"></td>
  </tr>
  <tr>
    <td><strong>Move a chess piece. Open a door.</strong><br>Reusable skills on a real MARS.</td>
    <td><strong>Find and pick up a sock.</strong><br>Give MARS a goal through the web app.</td>
  </tr>
</table>

MARS is the supported reference hardware today. Innate OS separates agents, skills, and inputs from the robot drivers; ports to other robots are welcome and require hardware integration. [MARS quick start →](https://docs.innate.bot/get-started/mars-quick-start)

## Try it without a robot

<p align="center">
  <a href="https://sim.innate.bot"><img src="docs/assets/readme/sim-agent.gif" alt="The MARS simulator and agent interface in action" width="720"></a>
</p>

**[Try the simulator →](https://sim.innate.bot)** — no install, no robot.

Or clone and run it locally on macOS, Linux, or WSL2. This installer clones the repository and sets up the simulator:

```bash
curl -fsSL https://link.innate.bot/sim | sh
cd innate-os
./innate-sim up
```

Open [https://localhost](https://localhost) to drive, run skills, and talk to the agent. [Simulator setup →](https://docs.innate.bot/simulator/setup)

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

### Example: An agent that remembers where it saw things

“Go to where you saw the guitar.” The agent searches memory, then navigates to the matching location.

```python
from innate import Agent, InputRef, SkillRef
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.search_memory import SearchMemory
from inputs.micro_input import MicroInput


class FindPlaceAgent(Agent):
    @property
    def id(self) -> str:
        return "find_place_agent"

    @property
    def display_name(self) -> str:
        return "Find a remembered place"

    def get_skills(self) -> list[SkillRef]:
        return [SearchMemory, NavigateToPosition]

    def get_inputs(self) -> list[InputRef]:
        return [MicroInput]

    def get_prompt(self) -> str:
        return (
            "When asked to find a place or object, search memory first. "
            "Navigate to the returned map coordinates with local_frame=false. "
            "If there is no match, ask the user to show you rather than guessing. "
            "On arrival, check the camera and report what you see. "
            "If the user says stop, stop immediately and do not retry."
        )
```

List skills and inputs as the classes themselves, so your editor catches a typo before the robot does.

Save it as `workspace/custom_agents/find_place_agent.py` and select **Find a remembered place** in the web app. Memory needs recorded views from the current map; if nothing matches, the agent asks you to show it.

Because the robot lives in the physical world, agents observe continuously and can interrupt a running skill when the world changes.

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

## LeRobot

MARS is a [LeRobot](https://github.com/huggingface/lerobot) robot. Record datasets while you teleoperate from the phone app or the web app, train any LeRobot policy on them, and run it back on the robot, with the standard commands and `--robot.type=mars`:

```bash
cd lerobot && uv sync
export MARS_HOST=192.168.1.42    # your robot's IP address
uv run hf auth login             # datasets upload to your Hugging Face account

uv run lerobot-record --robot.type=mars --robot.external_commands=true \
    --teleop.type=mars_passthrough --dataset.repo_id=YOUR_HF_NAME/mars-tidy-up \
    --dataset.single_task="Put the ball in the box" --dataset.num_episodes=10 \
    --dataset.private=true
```

You can also record in the web app or the phone app: the web app's Datasets page has a **Publish to Hugging Face** button that converts a recording to the same LeRobotDataset format.

**[MARS on LeRobot →](lerobot/README.md)**

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
- [MARS on LeRobot](lerobot/README.md)
- [Web app](https://docs.innate.bot/robots/web-app) and [controller app](https://docs.innate.bot/robots/innate-controller-app)
- [CLI](https://docs.innate.bot/software/innate-cli)
- [ROS 2 core and system overview](https://docs.innate.bot/software/ros2-core)

Most builders should start with skills, agents, inputs, and the simulator. Changing the ROS core is possible; it is not the usual path.

## License

Innate OS is open source under [Apache 2.0](LICENSE). Third-party code we ship is credited in [`NOTICE`](NOTICE).

The simulator's environments and characters are a separate matter: several are third-party works under CC BY 4.0, fetched at build time rather than stored here. They carry their own terms, listed per asset in [`sim/ATTRIBUTION.md`](sim/ATTRIBUTION.md).

## Contributing

We welcome contributions, apps built on Innate OS, and ports to other robots — we will be happy to feature them.

**[Contributing guide](.github/CONTRIBUTING.md)** · **[Discord](https://discord.gg/innate)**
