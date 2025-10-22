# Innate-OS Updates

## Usage

```bash
innate-update check   # Check for updates
innate-update apply   # Apply updates (asks confirmation)
innate-update status  # Show current version
```

## First Time Setup (Per Robot)

### 1. Install Command
```bash
sudo ln -s /home/jetson1/innate-os/scripts/update/innate-update /usr/local/bin/innate-update
```

### 2. Configure Sudo
```bash
sudo visudo
# Add: jetson1 ALL=(ALL) NOPASSWD: /home/jetson1/innate-os/scripts/update/post_update.sh
```

### 3. Setup GitHub App Authentication

**Get from GitHub App settings:**
- App ID (e.g., 123456)
- Installation ID (from install URL)
- Download private key (.pem file)

**Run setup:**
```bash
cd /home/jetson1/innate-os
./scripts/update/setup-github-app.sh <APP_ID> <INSTALLATION_ID> ~/path/to/key.pem
```

### 4. Optional: SSH Login Notification
```bash
echo "source /home/jetson1/innate-os/scripts/update/update_check.zsh" >> ~/.zshrc
```

## What Happens During Update

```
innate-update apply
  ↓
1. Shows changes, asks confirmation
  ↓
2. Stops tmux (ros_nodes) + systemd services
  ↓
3. Stashes local changes
  ↓
4. git pull --rebase
  ↓
5. Runs post_update.sh (as root):
   - Copies systemd files to /etc/systemd/system/
   - Updates scripts in /usr/local/bin/
   - Updates udev rules
   - Rebuilds ROS2 workspace (colcon build)
   - Installs pip requirements
   - Restarts services
  ↓
6. Done! System running new code
```

## Logs

```bash
tail -f logs/update.log
tail -f logs/post_update.log
```

## Rollback

```bash
cd /home/jetson1/innate-os
git log
git reset --hard <commit-hash>
sudo ./scripts/update/post_update.sh
```

## GitHub App Setup (Org Admin)

**Create App:** `github.com/organizations/innate-inc/settings/apps/new`

**Configure:**
- Name: "Innate OS Update"
- Permissions: Repository → Contents → **Read-only**
- Uncheck: Webhooks, User authorization
- Install on: innate-inc org
- Select: Only `maurice-prod` repo
- Generate private key

**Use:** Give App ID, Installation ID, and .pem file to robots.
