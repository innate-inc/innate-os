# Innate-OS Update System

## Table of Contents
- [Quick Start](#quick-start)
- [Deploy Key Setup](#deploy-key-setup)
- [Commands](#commands)
- [Configuration](#configuration)
- [Update Process](#update-process)
- [Service Management](#service-management)
- [Troubleshooting](#troubleshooting)
- [For Developers](#for-developers)

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

## Deploy Key Setup

Robots use SSH deploy keys for secure, read-only access to the release repository.

### For Administrators

Generate and install deploy keys for robots:

```bash
# Generate keys for N robots (run from dev machine)
cd /path/to/innate-os
export GITHUB_TOKEN=$(gh auth token)
./scripts/update/generate-deploy-keys.sh -n 40 -r innate-inc/innate-os-release --release innate-inc/innate-os-release

# Install key on a robot
cd deploy-keys
./install-key-on-robot.sh robot-001 jetson1@192.168.55.1
```

The install script will:
1. Remove old SSH keys (id_ed25519, id_rsa)
2. Install the deploy key
3. Configure SSH for GitHub
4. Switch to main branch and delete other branches
5. Update git remote to `innate-os-release`
6. Show final configuration report

### Managing Deploy Keys

```bash
# List all deploy keys on the repo
gh repo deploy-key list --repo innate-inc/innate-os-release

# Remove a specific key
gh repo deploy-key delete <KEY_ID> --repo innate-inc/innate-os-release

# Remove all keys
gh repo deploy-key list --repo innate-inc/innate-os-release --json id -q '.[].id' | \
  xargs -I {} gh repo deploy-key delete {} --repo innate-inc/innate-os-release --yes
```

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
| `INNATE_STATE_DIR` | `/var/lib/innate-update` | State/cache directory |
| `INNATE_UPDATE_BRANCH` | `main` | Git branch to track |
| `INNATE_UPDATE_INTERVAL` | `3600` | Daemon check interval (seconds) |

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
1. Git fetch with tags
       |
       v
2. Find latest tag
       |
       v
3. Git checkout to tag
       |
       v
4. Run post_update.sh:
   - Install apt dependencies
   - Install pip dependencies
   - Update systemd services
   - Update udev rules
   - Restart services
       |
       v
5. Start ROS services
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

**"Permission denied (publickey)"**
```bash
# Check if deploy key is installed
ls -la ~/.ssh/innate_deploy_key

# Test GitHub connection
ssh -T git@github.com

# Re-run install script if needed
./install-key-on-robot.sh robot-XXX jetson1@<ip>
```

**"No tags found"**
- Ensure the release repository has tagged releases
- Check git remote: `git remote -v`

**Update fails at post_update.sh**
```bash
# Run manually to see errors
sudo ~/innate-os/scripts/update/post_update.sh
```

### Rollback

```bash
cd ~/innate-os
git log --oneline --tags        # Find version to rollback to
git checkout 0.1.97             # Checkout specific version
sudo ./scripts/update/post_update.sh  # Rebuild and restart
```

---

## Shell Integration

Add update notifications to your shell prompt:

```bash
echo 'source ~/innate-os/scripts/update/update_check.zsh' >> ~/.zshrc
```

This shows a notification on login if updates are available.

---

## For Developers

### Release Workflow

When you push a tag to `innate-os`, GitHub Actions automatically pushes to `innate-os-release`:

```bash
# Create and push a new release
git tag 0.1.99
git push origin 0.1.99
```

The workflow (`.github/workflows/release-to-deploy-repo.yml`):
1. Checks out the tagged commit
2. Removes `.github/workflows` (deploy keys can't push workflows)
3. Commits as "Release X.Y.Z"
4. Pushes to `innate-os-release` with the tag

### Repository Structure

| Repository | Purpose | Access |
|------------|---------|--------|
| `innate-os` | Development repo | Developers only |
| `innate-os-release` | Customer-facing releases | Deploy keys (read-only) |

### Regenerating Deploy Keys

If you need to regenerate keys (e.g., key compromise):

```bash
# Remove all existing keys
gh repo deploy-key list --repo innate-inc/innate-os-release --json id -q '.[].id' | \
  xargs -I {} gh repo deploy-key delete {} --repo innate-inc/innate-os-release --yes

# Regenerate
./scripts/update/generate-deploy-keys.sh -n 40 -r innate-inc/innate-os-release --release innate-inc/innate-os-release

# Re-deploy to robots
./deploy-keys/install-key-on-robot.sh robot-001 jetson1@<ip>
```