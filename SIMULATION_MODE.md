# Simulation Mode Setup

This document explains how to run Innate OS in simulation mode vs hardware mode.

## Quick Start - Simulation

### 1. Start Docker/OrbStack
Make sure Docker or OrbStack is running on your Mac.

### 2. Build and Run
```bash
# Build the Docker image (defaults to simulation mode)
docker compose -f docker-compose.dev.yml build

# Start the containers
docker compose -f docker-compose.dev.yml up -d

# Enter the container
docker compose -f docker-compose.dev.yml exec innate zsh -l
```

### 3. Launch Simulation
Inside the container:
```bash
./scripts/launch_sim_in_tmux.zsh
```

This starts all services in a tmux session with organized windows:
- **Window 0 (zenoh)**: Zenoh router
- **Window 1 (rosbridge-app)**: Rosbridge + App control
- **Window 2 (webrtc)**: WebRTC transmitter
- **Window 3 (nav-brain)**: Navigation + Brain client
- **Window 4 (behavior)**: Arm manipulation

### 4. View in Browser
Access RViz2 via noVNC:
```
http://localhost:8080/vnc.html
```

### Tmux Commands
- Switch windows: `Ctrl+b` then `n`/`p` or `0-4`
- Detach: `Ctrl+b d`
- Reattach: `tmux attach-session -t stack`

## Switching Modes

### Simulation Mode (Default)
The default mode installs only common dependencies, skipping NVIDIA Jetson-specific packages.

**docker-compose.dev.yml:**
```yaml
services:
  innate:
    build:
      args:
        MODE: simulation
```

### Hardware Mode
For building images to run on physical robots (NVIDIA Jetson).

**docker-compose.dev.yml:**
```yaml
services:
  innate:
    build:
      args:
        MODE: hardware
```

Or use command line:
```bash
docker build --build-arg MODE=hardware -t innate-os .
```

## What's Different?

### Simulation Mode Includes:
- All ROS2 packages for navigation, manipulation, vision
- GStreamer for streaming
- Common development tools
- **Excludes**: NVIDIA VPI, NVIDIA L4T GStreamer
- **Skips**: Git update checks (no credentials needed)

### Hardware Mode Includes:
- Everything in simulation mode
- **Plus**: NVIDIA VPI for depth estimation
- **Plus**: NVIDIA L4T GStreamer for hardware acceleration
- **Plus**: Git update checks for system updates

## Dependency Files

See `ros2_ws/DEPENDENCIES_GUIDE.md` for details on:
- `apt-dependencies.common.txt` - Shared dependencies
- `apt-dependencies.hardware.txt` - Jetson-only dependencies

## Troubleshooting

### Docker daemon not running
```
Cannot connect to the Docker daemon at unix:///Users/.../.orbstack/run/docker.sock
```
**Solution**: Start OrbStack or Docker Desktop

### Build fails with "Unable to locate package nvidia-*"
**Solution**: You're trying to build in hardware mode on a non-Jetson system. Use `MODE=simulation` instead.

### Tmux session not found
```
error connecting to /tmp/tmux-*/default (No such file or directory)
```
**Solution**: The simulation hasn't been launched yet. Run `./scripts/launch_sim_in_tmux.zsh`

## Related Documentation

- [README.md](README.md) - Main project documentation
- [DEPENDENCIES_GUIDE.md](ros2_ws/DEPENDENCIES_GUIDE.md) - Dependency file details
- [SYSTEM_SETUP.md](SYSTEM_SETUP.md) - Full system setup guide
