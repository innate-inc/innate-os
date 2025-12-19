# Innate-OS Update System

## Table of Contents
- [Quick Start](#quick-start)
- [Installation](#installation)
  - [Install Script Options](#install-script-options)
  - [GitHub Token Setup](#github-token-setup)
- [Commands](#commands)
- [Running GitHub Actions with a Local Runner](#running-github-actions-with-a-local-runner)
- [Configuration](#configuration)
- [Update Process](#update-process)
- [Service Management](#service-management)
- [Troubleshooting](#troubleshooting)

---

## Quick Start

```bash
# Check for updates
innate-update check

# Apply updates (asks confirmation)
innate-update apply

# Show current version and service status
innate-update status

# Manage ROS services
innate-update start    # Start ROS services
innate-update stop     # Stop ROS services
innate-update restart  # Restart ROS services
innate-update view     # Attach to tmux session
```

---

## Installation

### Basic Installation (Public Repository)

```bash
curl -fsSL https://raw.githubusercontent.com/innate-inc/innate-os/main/install.sh | bash
```

### Private Repository Installation

```bash
GITHUB_TOKEN=ghp_your_token_here \
  curl -fsSL -H "Authorization: token ghp_your_token_here" \
  https://raw.githubusercontent.com/innate-inc/innate-os/main/install.sh | bash
```

### Install Script Options

The install script supports the following environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | *(none)* | GitHub Personal Access Token for private repos. **Token is saved automatically for future updates.** |
| `BUILD_FROM_SOURCE` | `false` | Set to `true` to build from source instead of downloading pre-built release artifacts |
| `INNATE_OS_DIR` | `~/innate-os` | Installation directory |
| `GITHUB_REPO` | `innate-inc/innate-os` | GitHub repository (owner/repo format) |

**Examples:**

```bash
# Install to custom directory
INNATE_OS_DIR=/opt/innate-os curl -fsSL .../install.sh | bash

# Force build from source
BUILD_FROM_SOURCE=true curl -fsSL .../install.sh | bash

# Private repo with token (token is saved automatically)
GITHUB_TOKEN=ghp_xxx curl -fsSL -H "Authorization: token ghp_xxx" \
  https://raw.githubusercontent.com/innate-inc/innate-os/main/install.sh | bash

# Use a fork
GITHUB_REPO=myorg/innate-os-fork curl -fsSL .../install.sh | bash
```

### GitHub Token Setup

For private repositories, you need a GitHub Personal Access Token (PAT).

#### Creating a Token

1. Go to GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Click "Generate new token (classic)"
3. Select scopes:
   - `repo` (Full control of private repositories)
4. Copy the token (starts with `ghp_`)

#### Token Persistence

The GitHub token is automatically saved and persists between launches:

- **During installation**: The token is saved to `~/.github_token`
- **During updates**: The `innate-update` command loads the saved token automatically
- **File permissions**: Token file is saved with `chmod 666` (read/write for all)

**To manually set or update the token:**

```bash
# Set token for current session
export GITHUB_TOKEN=ghp_your_token_here

# Save token permanently
echo "ghp_your_token_here" > ~/.github_token
chmod 666 ~/.github_token
```

**Token loading priority:**
1. `GITHUB_TOKEN` environment variable (if set)
2. Saved token from `~/.github_token`

---

## Commands

### Update Commands

```bash
innate-update check           # Check for updates (latest tagged release)
innate-update check --dev     # Check for updates (latest commit on main)
innate-update apply           # Download and apply latest release
innate-update apply --dev     # Update to latest commit (git-based)
innate-update status          # Show version info and service status
```

### Service Commands

```bash
innate-update start           # Start ROS services in tmux
innate-update stop            # Stop ROS services
innate-update restart         # Restart ROS services
innate-update view            # Attach to tmux session (Ctrl+b d to detach)
```

### Daemon Mode

```bash
innate-update daemon          # Run as background daemon (for systemd)
```

---

## Running GitHub Actions with a Local Runner

The build workflows use `runs-on: selfhosted` and require a local GitHub Actions runner.

### Setting Up a Self-Hosted Runner

1. **Go to your repository settings:**
   ```
   https://github.com/YOUR_ORG/innate-os/settings/actions/runners
   ```

2. **Click "New self-hosted runner"**

3. **Follow the setup instructions for your OS (Linux recommended):**

   ```bash
   # Create a directory for the runner
   mkdir actions-runner && cd actions-runner

   # Download the runner (get latest URL from GitHub)
   curl -o actions-runner-linux-x64-2.311.0.tar.gz -L \
     https://github.com/actions/runner/releases/download/v2.311.0/actions-runner-linux-x64-2.311.0.tar.gz

   # Extract
   tar xzf ./actions-runner-linux-x64-2.311.0.tar.gz

   # Configure (get token from GitHub UI)
   ./config.sh --url https://github.com/YOUR_ORG/innate-os --token YOUR_TOKEN

   # Install as service (optional, recommended)
   sudo ./svc.sh install
   sudo ./svc.sh start
   ```

4. **Verify runner is connected:**
   - Go to Settings → Actions → Runners
   - Runner should show as "Idle" with green status

### Requirements for the Runner Machine

The runner needs:
- **Docker** with buildx support
- **Git**
- **Sufficient disk space** (builds can be 10GB+)
- **Network access** to GitHub and Docker registries

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Verify Docker buildx
docker buildx version
```

### Available Workflows

| Workflow | File | Trigger | Description |
|----------|------|---------|-------------|
| Build Release | `build-release.yml` | Push to main, tags, PRs, manual | Builds ROS2 workspace, creates release archives |
| Docker Build | `docker-build.yml` | Push to main, tags, PRs, manual | Builds Docker image, pushes to ghcr.io |
| Test Pose Image | `test-pose-image.yml` | Changes to brain_client | Runs Python tests |

### Manually Triggering a Workflow

1. Go to Actions tab in your repository
2. Select the workflow
3. Click "Run workflow"
4. Select branch and click "Run workflow"

Or via CLI:
```bash
gh workflow run build-release.yml --ref main
gh workflow run docker-build.yml --ref main
```

### Viewing Workflow Logs

```bash
# List recent runs
gh run list

# View specific run
gh run view <run-id>

# Watch live
gh run watch
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `INNATE_OS_DIR` | `~/innate-os` | Installation directory |
| `INNATE_STATE_DIR` | `/var/lib/innate-update` | State/cache directory (tokens, cache) |
| `INNATE_UPDATE_BRANCH` | `main` | Git branch to track |
| `INNATE_UPDATE_INTERVAL` | `3600` | Daemon check interval (seconds) |
| `GITHUB_REPO` | `innate-inc/innate-os` | GitHub repository |
| `GITHUB_TOKEN` | *(from file)* | GitHub token (auto-loaded from saved file) |

### Token File Location

```
~/.github_token
```

### Update Cache

```
/var/lib/innate-update/update-cache
```

---

## Update Process

### Release Updates (Default)

```
innate-update apply
       |
       v
1. Fetch latest release from GitHub API
       |
       v
2. Download pre-built .tar.gz archive
       |
       v
3. Extract and replace files (preserves .git)
       |
       v
4. Update git checkout to release tag
       |
       v
5. Run post_update.sh:
   - Install apt dependencies
   - Install pip dependencies
   - Update systemd services
   - Update udev rules
   - Restart services
       |
       v
6. Start ROS services
```

### Development Updates (--dev)

```
innate-update apply --dev
       |
       v
1. Git fetch origin
       |
       v
2. Stash local changes
       |
       v
3. Git checkout origin/main
       |
       v
4. Run post_update.sh
       |
       v
5. Start ROS services
```

---

## Service Management

### Systemd Services

```bash
# Update daemon (checks for updates periodically)
sudo systemctl status innate-update.service
sudo systemctl start innate-update.service
sudo systemctl enable innate-update.service

# Discovery server
sudo systemctl status discovery-server.service

# ROS app
sudo systemctl status ros-app.service

# BLE provisioner
sudo systemctl status ble-provisioner.service
```

### Tmux Sessions

```bash
# List sessions
tmux ls

# Attach to ROS nodes
tmux attach -t ros_nodes

# Navigation in tmux
# Ctrl+b then arrow keys - switch panes
# Ctrl+b then d - detach
```

---

## Troubleshooting

### View Logs

```bash
# Update logs
tail -f ~/innate-os/logs/update.log

# Post-update logs
tail -f ~/innate-os/logs/post_update.log

# Systemd service logs
journalctl -u innate-update.service -f
journalctl -u ros-app.service -f
```

### Common Issues

**"Bad credentials" or "Requires authentication"**
```bash
# Check if token is set
echo $GITHUB_TOKEN

# Re-save token
echo "ghp_your_new_token" > ~/.github_token
chmod 666 ~/.github_token
```

**"No releases found"**
- Ensure the repository has at least one release with `.tar.gz` assets
- Check you're using the correct `GITHUB_REPO`

**Update fails at post_update.sh**
```bash
# Run manually to see errors
sudo ~/innate-os/scripts/update/post_update.sh
```

### Rollback

```bash
cd ~/innate-os
git log --oneline --tags        # Find version to rollback to
git checkout v1.2.3             # Checkout specific version
sudo ./scripts/update/post_update.sh  # Rebuild and restart
```

### Reset Token

```bash
# Remove saved token
rm ~/.github_token

# Set new token
echo "ghp_new_token_here" > ~/.github_token
chmod 666 ~/.github_token
```

---

## Shell Integration

Add update notifications to your shell prompt:

```bash
echo 'source ~/innate-os/scripts/update/update_check.zsh' >> ~/.zshrc
```

This shows a notification on login if updates are available.

---

## GitHub App Authentication (Alternative)

For organizations preferring GitHub App authentication over personal tokens:

1. Create a GitHub App with repository access
2. Get: App ID, Installation ID, Private key (.pem)
3. Run setup:
   ```bash
   ./scripts/update/setup-github-app.sh <APP_ID> <INSTALLATION_ID> ~/path/to/key.pem
   ```

This generates short-lived tokens automatically.