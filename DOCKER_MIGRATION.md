# Docker Migration Guide

This document outlines the remaining steps to complete the Docker-based deployment for Innate OS.

## Completed Steps

- [x] `Dockerfile.prod` - Production Dockerfile for ghcr.io
- [x] `docker-compose.prod.yml` - Multi-service docker-compose configuration
- [x] `.github/workflows/docker-build.yml` - CI workflow to build and push to ghcr.io

## Remaining Steps

### 1. Create Docker-based Updater Script

Create `scripts/update/innate-update-docker` - a Python script to manage Docker-based updates.

```python
#!/usr/bin/env python3
"""
Innate-OS Docker Update Manager

Usage:
    innate-update check    - Check for new Docker image versions
    innate-update apply    - Pull new image and restart containers
    innate-update status   - Show current running version
    innate-update daemon   - Run as background daemon
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

class Config:
    def __init__(self):
        self.compose_dir = Path(os.environ.get('INNATE_OS_DIR', Path.home() / 'innate-os'))
        self.compose_file = self.compose_dir / 'docker-compose.prod.yml'
        self.state_dir = Path(os.environ.get('INNATE_STATE_DIR', '/var/lib/innate-update'))
        self.state_file = self.state_dir / 'state.json'
        self.github_repo = os.environ.get('GITHUB_REPO', 'innate-inc/innate-os')
        self.registry = os.environ.get('INNATE_REGISTRY', 'ghcr.io')
        self.image_name = os.environ.get('INNATE_IMAGE', f'{self.registry}/{self.github_repo}')
        self.poll_interval = int(os.environ.get('INNATE_UPDATE_INTERVAL', '3600'))

        self.state_dir.mkdir(parents=True, exist_ok=True)

class DockerUpdater:
    def __init__(self, config: Config):
        self.config = config

    def _run(self, cmd: list, check=True) -> subprocess.CompletedProcess:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise Exception(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
        return result

    def load_state(self) -> dict:
        if self.config.state_file.exists():
            with open(self.config.state_file) as f:
                return json.load(f)
        return {}

    def save_state(self, state: dict):
        state['updated_at'] = datetime.utcnow().isoformat()
        with open(self.config.state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def get_remote_manifest(self) -> dict:
        """Fetch manifest.json from latest GitHub release"""
        api_url = f"https://api.github.com/repos/{self.config.github_repo}/releases/latest"
        req = Request(api_url, headers={"Accept": "application/vnd.github+json"})

        with urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode())

        # Find manifest.json asset
        for asset in release.get('assets', []):
            if asset['name'] == 'manifest.json':
                with urlopen(asset['browser_download_url'], timeout=30) as resp:
                    return json.loads(resp.read().decode())

        # Fallback: construct from release info
        return {
            'version': release['tag_name'],
            'docker': {
                'image': self.config.image_name,
                'tag': release['tag_name']
            }
        }

    def get_running_version(self) -> str:
        """Get currently running container image version"""
        result = self._run([
            'docker', 'inspect',
            '--format', '{{index .Config.Labels "org.opencontainers.image.version"}}',
            'innate-discovery'
        ], check=False)
        return result.stdout.strip() if result.returncode == 0 else 'unknown'

    def check(self) -> bool:
        """Check if update is available"""
        print("Checking for updates...")

        manifest = self.get_remote_manifest()
        remote_version = manifest.get('version', 'unknown')
        local_version = self.get_running_version()

        print(f"  Running: {local_version}")
        print(f"  Latest:  {remote_version}")

        if remote_version != local_version and remote_version != 'unknown':
            print(f"\n[UPDATE AVAILABLE] {local_version} -> {remote_version}")
            print("Run 'innate-update apply' to install")
            return True
        else:
            print("\n[UP TO DATE]")
            return False

    def apply(self) -> int:
        """Pull new image and restart containers"""
        print("\n" + "="*50)
        print("  INNATE-OS UPDATE")
        print("="*50 + "\n")

        manifest = self.get_remote_manifest()
        image = manifest['docker']['image']
        tag = manifest['docker']['tag']
        full_image = f"{image}:{tag}"

        print(f"[1/3] Pulling image: {full_image}")
        self._run(['docker', 'pull', full_image])

        print(f"[2/3] Updating .env with new version...")
        env_file = self.config.compose_dir / '.env'
        env_content = f"INNATE_VERSION={tag}\n"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    if not line.startswith('INNATE_VERSION='):
                        env_content += line
        with open(env_file, 'w') as f:
            f.write(env_content)

        print("[3/3] Restarting containers...")
        self._run([
            'docker', 'compose',
            '-f', str(self.config.compose_file),
            'down'
        ])
        self._run([
            'docker', 'compose',
            '-f', str(self.config.compose_file),
            'up', '-d'
        ])

        # Save state
        self.save_state({
            'version': tag,
            'image': full_image,
            'commit': manifest.get('commit', 'unknown')
        })

        print("\n" + "="*50)
        print(f"  UPDATE COMPLETE: {tag}")
        print("="*50 + "\n")
        return 0

    def status(self):
        """Show current status"""
        state = self.load_state()
        running = self.get_running_version()

        print("Innate-OS Status:")
        print(f"  Running Version: {running}")
        print(f"  Installed Image: {state.get('image', 'unknown')}")
        print(f"  Last Updated:    {state.get('updated_at', 'never')}")

    def daemon(self):
        """Run as background daemon"""
        print(f"Starting update daemon (interval: {self.config.poll_interval}s)")

        while True:
            try:
                if self.check():
                    print("Update available, applying...")
                    self.apply()
            except Exception as e:
                print(f"Error: {e}", file=sys.stderr)

            time.sleep(self.config.poll_interval)

def main():
    parser = argparse.ArgumentParser(description='Innate-OS Docker Update Manager')
    parser.add_argument('command', choices=['check', 'apply', 'status', 'daemon'])
    args = parser.parse_args()

    config = Config()
    updater = DockerUpdater(config)

    commands = {
        'check': lambda: sys.exit(0 if not updater.check() else 1),
        'apply': updater.apply,
        'status': updater.status,
        'daemon': updater.daemon,
    }

    commands[args.command]()

if __name__ == '__main__':
    main()
```

### 2. Create Systemd Service for Docker Compose

Create `systemd/innate-docker.service`:

```ini
[Unit]
Description=Innate OS Docker Services
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=jetson1
Group=jetson1
WorkingDirectory=/home/jetson1/innate-os
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.prod.yml down
ExecReload=/usr/bin/docker compose -f docker-compose.prod.yml restart
TimeoutStartSec=300
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

### 3. Create Updater Daemon Service

Create `systemd/innate-update-docker.service`:

```ini
[Unit]
Description=Innate OS Docker Update Daemon
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=jetson1
Group=jetson1
ExecStart=/home/jetson1/innate-os/scripts/update/innate-update-docker daemon
Restart=always
RestartSec=60
Environment="INNATE_OS_DIR=/home/jetson1/innate-os"
Environment="INNATE_UPDATE_INTERVAL=3600"

[Install]
WantedBy=multi-user.target
```

### 4. Update DDS Setup Script

The `dds/setup_dds.sh` can be simplified for Docker since we use host networking and static 127.0.0.1:

```bash
#!/bin/bash
# DDS Setup Script - Simplified for Docker with host networking

export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID=0
export ROS_DISCOVERY_SERVER=127.0.0.1:11811
export FASTRTPS_DEFAULT_PROFILES_FILE=${INNATE_OS_ROOT:-/root/innate-os}/dds/super_client_configuration.xml

echo "DDS configured: Discovery Server at $ROS_DISCOVERY_SERVER" >&2
```

### 5. Create .env.template for Docker

Update `.env.template` with Docker-specific variables:

```bash
# Innate OS Docker Configuration

# Docker image version (set by updater)
INNATE_VERSION=latest

# API Keys
CARTESIA_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=

# Optional overrides
# INNATE_REGISTRY=ghcr.io
# GITHUB_REPO=innate-inc/innate-os
```

---

## Installation on Robot

Once all files are created, deploy to the robot:

```bash
# 1. Install Docker if not present
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 2. Clone or copy the innate-os directory
git clone https://github.com/innate-inc/innate-os.git ~/innate-os
cd ~/innate-os

# 3. Create .env file from template
cp .env.template .env
# Edit .env and add API keys

# 4. Pull the Docker image
docker compose -f docker-compose.prod.yml pull

# 5. Start services
docker compose -f docker-compose.prod.yml up -d

# 6. Install systemd services (optional, for boot startup)
sudo cp systemd/innate-docker.service /etc/systemd/system/
sudo cp systemd/innate-update-docker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable innate-docker.service
sudo systemctl enable innate-update-docker.service
```

---

## Files That Become Obsolete

Once Docker migration is complete, these files are no longer needed for production:

| File | Reason |
|------|--------|
| `scripts/launch_ros_in_tmux.sh` | Replaced by docker-compose services |
| `systemd/ros-app.service` | Replaced by innate-docker.service |
| `systemd/discovery-server.service` | Discovery runs in container |
| `scripts/update/post_update.sh` | Everything baked into Docker image |
| `scripts/update/innate-update` | Replaced by innate-update-docker |

**Keep these files** for non-Docker deployments or development.

---

## Useful Commands

```bash
# View all container logs
docker compose -f docker-compose.prod.yml logs -f

# View specific service logs
docker compose -f docker-compose.prod.yml logs -f app-bringup

# Restart a specific service
docker compose -f docker-compose.prod.yml restart brain-nav

# Stop all services
docker compose -f docker-compose.prod.yml down

# Pull latest images and restart
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d

# Check running containers
docker compose -f docker-compose.prod.yml ps

# Execute command in a running container
docker compose -f docker-compose.prod.yml exec app-bringup bash

# Check resource usage
docker stats
```

---

## Troubleshooting

### Containers can't communicate (DDS issues)
- Ensure all containers use `network_mode: host`
- Check discovery-server is healthy: `docker compose ps discovery-server`
- Verify environment variables: `docker compose exec app-bringup env | grep ROS`

### Hardware not accessible
- Ensure `privileged: true` is set
- Check device permissions: `ls -la /dev/ttyACM0 /dev/rplidar`
- User must be in `dialout` group: `sudo usermod -aG dialout $USER`

### Image pull fails
- Check GitHub authentication: `docker login ghcr.io`
- Verify image exists: `docker manifest inspect ghcr.io/innate-inc/innate-os:latest`

### Container exits immediately
- Check logs: `docker compose logs <service-name>`
- Ensure workspace was built: `docker compose exec <service> ls /root/innate-os/ros2_ws/install`
