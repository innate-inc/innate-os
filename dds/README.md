# DDS / Zenoh Connection Guide

ROS2 communication on Maurice uses [Zenoh](https://zenoh.io/) via `rmw_zenoh_cpp` instead of the default Fast-DDS middleware. This enables cross-machine ROS2 communication without multicast.

## How It Works

Every ROS node connects to a local **Zenoh router** (`rmw_zenohd`). To link two machines (e.g. your laptop and a robot), run a **satellite router** on your machine that connects to the robot's router over TCP.

```
[Your Laptop]                    [Robot]
  ROS nodes                       ROS nodes
      |                               |
  satellite router  ──TCP──>  main router (port 7447)
```

## Setup

### 1. Source the DDS environment

In every terminal where you run ROS nodes:

```zsh
source ~/innate-os/dds/setup_dds.zsh
```

This sets:
- `RMW_IMPLEMENTATION=rmw_zenoh_cpp` — use Zenoh instead of Fast-DDS
- `ROS_DOMAIN_ID=0`
- Shared memory transport (12 MiB buffer per node)
- Drop-wait tuning for high-throughput topics (cameras)

### 2. Start the Zenoh router

**On the robot** (or any machine running as the "hub"), the router is managed by systemd:

```bash
sudo systemctl start zenoh-router
sudo systemctl enable zenoh-router   # start on boot
sudo systemctl status zenoh-router   # check logs
```

**To start manually:**

```zsh
~/innate-os/dds/start_zenoh_router.zsh
```

### 3. Connect from another machine (satellite mode)

To link your laptop to a robot over the network:

```zsh
~/innate-os/dds/start_zenoh_router.zsh <robot-ip>
```

Example:

```zsh
~/innate-os/dds/start_zenoh_router.zsh 192.168.1.42
```

The script checks reachability on port 7447 before connecting. If it can't reach the robot, it exits with an error.

Then **in every other terminal** on your laptop, source the DDS env:

```zsh
source ~/innate-os/dds/setup_dds.zsh
```

Now `ros2 topic list`, `ros2 run`, etc. will see topics from both machines.

## Troubleshooting

**Can't see robot topics:**
- Confirm the robot's zenoh router is running: `sudo systemctl status zenoh-router`
- Check port 7447 is reachable: `nc -z -w 5 <robot-ip> 7447`
- Make sure you sourced `setup_dds.zsh` in every terminal

**Nodes can't communicate on the same machine:**
- Make sure the local zenoh router is running (even for single-machine use)
- Run `ros2 daemon stop` then restart your nodes

**High CPU / latency on camera topics:**
- Shared memory is enabled by default (12 MiB/node). For nodes that don't need it, override `ZENOH_SESSION_CONFIG_OVERRIDE` with `transport/shared_memory/enabled=false`.
