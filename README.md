<div align="center">

# Innate OS

## A lightweight, agentic, ROS2-based operating system for Innate robots

[![Discord](https://img.shields.io/badge/Discord-Join%20our%20community-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/innate)
[![Documentation](https://img.shields.io/badge/Docs-Read%20the%20docs-blue?style=for-the-badge&logo=readthedocs&logoColor=white)](https://docs.innate.bot)
[![Website](https://img.shields.io/badge/Website-Visit%20us-orange?style=for-the-badge&logo=safari&logoColor=white)](https://innate.bot)
[![ROS2](https://img.shields.io/badge/ROS2-Humble-22314E?style=for-the-badge&logo=ros&logoColor=white)](https://docs.ros.org/en/humble/)

</div>

> [!NOTE]
> **This OS is in active development.** APIs and features may change. Join our Discord for updates and support.

## Overview

The Innate OS provides the runtime environment for Innate robots. Developers can create powerful spatial applications using mobility, manipulation, interaction, and planning.

It is designed on a few **core principles**:

- Lightweight: The Innate OS can run on resource-constrained hardware like the Jetson Orin Nano 8GB. It starts in an under a minute.
- Intuitive: Creating a program should be easy and not need to touch ROS at all. And the OS can be controlled via the Innate phone app.
- Powerful: The Innate OS can be quickly extended to perform long-range tasks with robots. It natively supports VLAs and agentic workflows.

## Quick Start (Simulation)

If you don't have a robot, you can simply start the Innate OS in simulation mode, then use [our simulator](https://github.com/innate-inc/genesis-sim) to try out the robot with a web interface.

First build the container:

```bash
docker compose -f docker-compose.dev.yml build
```

Then run the container:

```bash
docker compose -f docker-compose.dev.yml up -d
```

And then drop into the container:

```bash
docker compose -f docker-compose.dev.yml exec innate zsh -l
```

See the nodes running with tmux
```
tmux a
```

You can use novnc to connect to rviz2. After launching rviz2 inside the container, you can connect to the instance in your browser:

```bash
http://localhost:8080/vnc.html
```

Inside the container, first run the discovery service:

```bash
discovery-and-launch-sim
```

VERIFY THAT THE IP ADDRESS IS CORRECT IN SETUP_DDS.ZSH

Then join the tmux session:

```bash
tmux a
```

Then run the simulation in a new tmux pane:

```bash
ros2 launch maurice_sim_bringup sim_rosbridge.launch.py
```

The run the nav system in a new tmux pane:

```bash
ros2 launch maurice_nav maurice_nav_launch.py
```

Then run the brain client in a new tmux pane:

```bash
ros2 launch brain_client brain_client.launch.py
```


## Quick start (Physical Robot)

Simply SSH into the robot. 

- If it's the first time and you're installing it, clone the repository and execute the post_update.sh script to complete the setup.

- Execute the launch_ros_in_tmux.sh script to start the ROS nodes.

Connect via the app like explained in the [documentation](https://docs.innate.bot).