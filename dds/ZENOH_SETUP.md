# Zenoh Middleware Setup for ROS2

## Overview

Zenoh is an alternative RMW (ROS Middleware) implementation that can replace FastDDS. It's lightweight, peer-to-peer, and often provides better performance.

## Changes Made

### 1. DDS Setup Script (`setup_dds.zsh`)

Changed the RMW implementation:

```bash
# FastDDS (original)
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp

# Zenoh (new)
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
```

### 2. New Systemd Service (`/etc/systemd/system/zenoh-router.service`)

Created to run the Zenoh router daemon on boot:

```ini
[Unit]
Description=Zenoh Router for ROS2
After=network-online.target
Wants=network-online.target

[Service]
User=jetson1
Group=jetson1
ExecStart=/bin/zsh -c "source /opt/ros/humble/setup.zsh && ros2 run rmw_zenoh_cpp rmw_zenohd"
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

## Switching Between Zenoh and FastDDS

### To use Zenoh:

1. Edit `~/innate-os/dds/setup_dds.zsh`:
   ```bash
   export RMW_IMPLEMENTATION=rmw_zenoh_cpp
   ```

2. Enable Zenoh service, disable FastDDS:
   ```bash
   sudo systemctl enable zenoh-router.service
   sudo systemctl disable discovery-server.service
   ```

3. Reboot or restart:
   ```bash
   sudo systemctl restart ros-app.service
   ```

### To use FastDDS:

1. Edit `~/innate-os/dds/setup_dds.zsh`:
   ```bash
   export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
   ```

2. Enable FastDDS service, disable Zenoh:
   ```bash
   sudo systemctl enable discovery-server.service
   sudo systemctl disable zenoh-router.service
   ```

3. Reboot or restart:
   ```bash
   sudo systemctl restart ros-app.service
   ```

## Troubleshooting

### Can't see nodes with `ros2 node list`

Your terminal's ROS2 daemon may be using a different RMW. Fix:

```bash
ros2 daemon stop
ros2 daemon start
ros2 node list
```

### Check which RMW is active

```bash
echo $RMW_IMPLEMENTATION
```

### Check if Zenoh router is running

```bash
ps aux | grep zenohd
systemctl status zenoh-router.service
```

### View Zenoh router logs

```bash
journalctl -u zenoh-router.service -f
```