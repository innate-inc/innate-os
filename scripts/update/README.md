# Innate-OS Updates

## Quick Start

```bash
# Check for updates
innate-update check

# Apply updates (asks confirmation)
innate-update apply

# Show current version
innate-update status
```

## Installation

The easiest way to install is using the installer script:

```bash
curl -fsSL https://raw.githubusercontent.com/innate-inc/innate-os/main/install.sh | bash
```

This will:
1. Install ROS2 Humble
2. Clone the repository
3. Install all dependencies
4. Build the ROS2 workspace
5. Install the updater daemon

### Manual Installation

```bash
# Install the command
sudo cp /path/to/innate-os/scripts/update/innate-update /usr/local/bin/innate-update
sudo chmod +x /usr/local/bin/innate-update

# Configure sudo for post-update script
echo "$USER ALL=(ALL) NOPASSWD: /path/to/innate-os/scripts/update/post_update.sh" | sudo tee /etc/sudoers.d/innate-update
sudo chmod 440 /etc/sudoers.d/innate-update
```

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `INNATE_OS_DIR` | `/home/$USER/innate-os` | Installation directory |
| `INNATE_UPDATE_BRANCH` | `main` | Git branch to track |
| `INNATE_UPDATE_INTERVAL` | `3600` | Daemon check interval (seconds) |

## Commands

```bash
innate-update check         # Check if updates are available (latest tag)
innate-update check --dev   # Check for updates (latest commit)
innate-update apply         # Apply pending updates (to latest tag)
innate-update apply --dev   # Apply updates (to latest commit)
innate-update status        # Show current version info
innate-update daemon        # Run as background daemon (for systemd)
```

By default, updates are to the latest tagged release on main.
Use `--dev` flag to update to the latest commit instead.

## Update Process

```
innate-update apply
  |
  v
1. Shows changes, asks confirmation
  |
  v
2. Stops tmux (ros_nodes) + systemd services
  |
  v
3. Stashes local changes
  |
  v
4. git checkout <target>
  |
  v
5. Runs post_update.sh (as root):
   - Installs apt dependencies (from ros2_ws/apt-dependencies.txt)
   - Installs pip dependencies (from ros2_ws/pip-requirements.txt)
   - Copies systemd files to /etc/systemd/system/
   - Updates scripts in /usr/local/bin/
   - Updates udev rules
   - Rebuilds ROS2 workspace (colcon build)
   - Restarts services
  |
  v
6. Done! System running new code
```

## Dependency Management

Dependencies are managed via config files in `ros2_ws/`:

- **`apt-dependencies.txt`** - System packages (installed via apt)
- **`pip-requirements.txt`** - Python packages (installed via pip3)

These files are used by:
- The installer script
- The post-update script
- The Docker build process

## Daemon Mode

The updater can run as a systemd service to periodically check for updates:

```bash
# Start the daemon
sudo systemctl start innate-update.service

# Enable on boot
sudo systemctl enable innate-update.service

# View logs
journalctl -u innate-update.service -f
```

## Logs

```bash
tail -f ~/innate-os/logs/update.log
tail -f ~/innate-os/logs/post_update.log
```

## Rollback

```bash
cd ~/innate-os
git log                           # Find the commit/tag to rollback to
git checkout <commit-or-tag>      # Checkout the version
sudo ./scripts/update/post_update.sh  # Rebuild and restart
```

## Shell Integration

Add update notifications to your shell:

```bash
echo "source ~/innate-os/scripts/update/update_check.zsh" >> ~/.zshrc
```

This will show a notification on login if updates are available.

## GitHub App Authentication (Optional)

For private repositories, set up GitHub App authentication:

1. Get from GitHub App settings:
   - App ID
   - Installation ID
   - Private key (.pem file)

2. Run setup:
   ```bash
   ./scripts/update/setup-github-app.sh <APP_ID> <INSTALLATION_ID> ~/path/to/key.pem
   ```
