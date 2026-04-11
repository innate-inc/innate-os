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
- Powerful: The Innate OS can be quickly extended to perform long-range tasks with robots. It natively supports VLAs, VLNs, and agentic workflows.

## Quick start (Physical Robot)

- Simply SSH into your MARS and connect via the app like explained in the [documentation](https://docs.innate.bot).

- Execute `innate build` to build your changes, `innate restart` to restart the ROS nodes, or `innate update apply` to update it to the latest stable version.


## ROS Packages

The main ROS packages that make up the runtime. See [scripts/launch_ros_in_tmux.sh](scripts/launch_ros_in_tmux.sh) for how they are wired together at startup.

- **[maurice_control](ros2_ws/src/maurice_bot/maurice_control)** — top-level robot app node, rosbridge websocket server for the mobile/web app, and low-latency UDP receiver for leader-arm teleop.
- **[maurice_bringup](ros2_ws/src/maurice_bot/maurice_bringup)** — hardware bringup for motors, base, IMU, and LiDAR, plus `robot_state_publisher` for the TF tree.
- **[maurice_arm](ros2_ws/src/maurice_bot/maurice_arm)** — arm + head servo driver, MoveIt `move_group`, and KDL-based IK solver.
- **[maurice_cam](ros2_ws/src/maurice_bot/maurice_cam)** — stereo main camera, arm camera, VPI stereo depth estimator, WebRTC streamer, and stereo calibration action server.
- **[maurice_nav](ros2_ws/src/maurice_bot/maurice_nav)** — Nav2-based navigation, SLAM mapping, and the mode manager that switches between `mapfree` / `mapping` / `navigation`.
- **[brain_client](ros2_ws/src/brain/brain_client)** — bridges the robot to the cloud brain (agent-v1.innate.bot): websocket client, skills action server, and the user input manager (STT/TTS).
- **[manipulation](ros2_ws/src/brain/manipulation)** — records/replays manipulation demonstrations and runs either learned or scripted manipulation policies.
- **[innate_logger](ros2_ws/src/cloud/innate_logger)** — uploads robot logs and telemetry to the Innate cloud.
- **[innate_training_node](ros2_ws/src/cloud/innate_training_node)** — collects training episodes and pushes them to the training cloud.
- **[innate_uninavid](ros2_ws/src/cloud/innate_uninavid)** — Uninavid vision-language navigation client (talks to `nav-v1.innate.bot`).
