# Dependencies Guide

This directory contains mode-specific dependency files for the Innate OS ROS2 workspace.

## Files

### `apt-dependencies.common.txt`
Common system dependencies required for **both simulation and hardware modes**.
This includes ROS2 packages, GStreamer, Python packages, and navigation/manipulation tools.

### `apt-dependencies.hardware.txt`
Hardware-specific dependencies **only for physical robots** (NVIDIA Jetson).
This includes:
- `nvidia-vpi-dev` - NVIDIA Vision Programming Interface for stereo depth
- `nvidia-l4t-gstreamer` - NVIDIA hardware-accelerated GStreamer plugins

### `apt-dependencies.txt` (Deprecated)
Legacy file kept for backwards compatibility. Points to the new mode-specific files.

## Usage

### Local Simulator

For simulator development, use the launcher from the repo root:

```bash
./innate sim setup
./innate sim up
```

The launcher builds or pulls the appropriate simulation image and keeps Docker,
ROS validation, and runtime readiness checks in one path.

### Docker Image Development

The Dockerfile still supports explicit mode selection when you are working on
the image itself:

**Hardware Mode (for physical robots):**
```bash
docker build --build-arg MODE=hardware -t innate-os .
```

### Manual Installation

**For Simulation (Mac/PC):**
```bash
cd ros2_ws
xargs sudo apt-get install -y < apt-dependencies.common.txt
```

**For Physical Robot (Jetson):**
```bash
cd ros2_ws
cat apt-dependencies.common.txt apt-dependencies.hardware.txt | \
  grep -v '^#' | grep -v '^$' | \
  xargs sudo apt-get install -y
```

Or install separately:
```bash
xargs sudo apt-get install -y < apt-dependencies.common.txt
xargs sudo apt-get install -y < apt-dependencies.hardware.txt
```

## Why Separate Files?

- **Cleaner builds**: Simulation environments don't need Jetson-specific packages
- **Cross-platform support**: Build on Mac, Linux, or ARM without modification
- **Faster iterations**: Skip unnecessary hardware packages in development
- **Clear separation**: Easy to see which dependencies are platform-specific

## Migration Notes

If you have scripts or documentation referencing `apt-dependencies.txt`, they will continue to work but should be updated to use the mode-specific files for better clarity.
