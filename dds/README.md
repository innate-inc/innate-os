# DDS / Zenoh Setup

innate-os uses [rmw_zenoh_cpp](https://github.com/ros2/rmw_zenoh) as the ROS 2 middleware (replacing the default FastDDS). All ROS nodes communicate through a local Zenoh router.

## How It Works

Each machine runs a **Zenoh router** (`rmw_zenohd`) that all ROS nodes connect to. This is the hub. Optionally, a second machine can run in **satellite mode**, connecting its router to the hub's router to bridge two machines.

```
  [ROS node] ──┐
  [ROS node] ──┼──► [Zenoh router] ◄──── (satellite router on another machine)
  [ROS node] ──┘
```

## Setup — Every Terminal

Before running any ROS command, source the environment setup:

```zsh
source <path-to-innate-os>/dds/setup_dds.zsh
```

This sets:
- `RMW_IMPLEMENTATION=rmw_zenoh_cpp` — tells ROS to use Zenoh
- `ROS_DOMAIN_ID=0`
- `ZENOH_ROUTER_CHECK_ATTEMPTS=0` — nodes wait indefinitely for the router to start
- Shared memory transport with a 12 MiB buffer per node (speeds up large messages like camera frames on localhost)

> **Tip:** Add this to your `~/.zshrc` or `~/.bashrc` so it's always active.

## Starting the Zenoh Router

### Via systemd (recommended on the robot)

The router is managed by a systemd service. To check its status:

```bash
systemctl status zenoh-router
```

Start / stop / restart:

```bash
sudo systemctl start zenoh-router
sudo systemctl stop zenoh-router
sudo systemctl restart zenoh-router
```

### Manually

```zsh
./dds/start_zenoh_router.zsh
```

## Satellite Mode (Cross-Machine / Debugging)

To connect a second machine (e.g. your laptop) to a running robot, start the router in satellite mode, passing the robot's IP:

```zsh
./dds/start_zenoh_router.zsh <robot-ip>
```

This:
1. Checks that port `7447` on the robot is reachable
2. Stops any stray ROS daemon
3. Starts a Zenoh router that connects to `tcp/<robot-ip>:7447`

Then source `setup_dds.zsh` in every terminal on the laptop — ROS nodes will automatically route through the bridge.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `ros2 topic list` is empty | Is the router running? `systemctl status zenoh-router` |
| Nodes time out waiting for router | `ZENOH_ROUTER_CHECK_ATTEMPTS=0` should be set — did you source `setup_dds.zsh`? |
| Satellite mode fails: "Cannot connect" | Firewall on the robot? Port 7447 must be open. Try `nc -z <robot-ip> 7447` |
| High memory use | Each node allocates a 12 MiB shared memory pool. Expected; tune `pool_size` in `setup_dds.zsh` if needed. |
