#!/bin/bash
# Post-Update Script for Innate-OS
# This script runs after git pull to update system components
# Requires root privileges via sudo

set -e  # Exit on error

# Check for root privileges
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root. Please use sudo." >&2
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
LOG_FILE="$REPO_DIR/logs/post_update.log"

# Create logs directory if it doesn't exist
mkdir -p "$(dirname "$LOG_FILE")"

# Logging function
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Ensure log file has correct ownership
ensure_log_ownership() {
    if [ -f "$LOG_FILE" ]; then
        # Get the actual user who started the script (not the sudo user)
        ACTUAL_USER=${SUDO_USER:-$USER}
        chown "$ACTUAL_USER:$ACTUAL_USER" "$LOG_FILE" 2>/dev/null || true
    fi
}

log "========================================"
log "Starting post-update script"
log "Repository: $REPO_DIR"
log "========================================"

# Stop running services before updating
log "Stopping services to begin update..."

# Kill tmux sessions if running
ACTUAL_USER=${SUDO_USER:-$USER}
if sudo -u "$ACTUAL_USER" tmux has-session -t ros_nodes 2>/dev/null; then
    log "Stopping tmux session: ros_nodes"
    sudo -u "$ACTUAL_USER" tmux kill-session -t ros_nodes
fi

# Stop systemd services
for service in discovery-server.service ros-app.service; do
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        log "Stopping $service"
        systemctl stop "$service"
    fi
done

log "All services stopped."

# 1. Update systemd service files if changed
log "Checking systemd service files..."
if [ -d "$REPO_DIR/systemd" ]; then
    for service_file in "$REPO_DIR/systemd"/*.service; do
        if [ -f "$service_file" ]; then
            service_name=$(basename "$service_file")
            log "Copying $service_name to /etc/systemd/system/"
            cp "$service_file" /etc/systemd/system/
        fi
    done
    systemctl daemon-reload
    log "Systemd daemon reloaded"
fi

# 2. Update scripts in /usr/local/bin if they changed
log "Checking helper scripts..."
if [ -d "$REPO_DIR/scripts" ]; then
    # Copy restart script if it exists
    if [ -f "$REPO_DIR/scripts/restart_robot_networking.sh" ]; then
        log "Updating restart_robot_networking.sh"
        cp "$REPO_DIR/scripts/restart_robot_networking.sh" /usr/local/bin/
        chmod +x /usr/local/bin/restart_robot_networking.sh
    fi
    
    # Copy tmux launcher if it exists
    if [ -f "$REPO_DIR/scripts/launch_ros_in_tmux.sh" ]; then
        log "Updating launch_ros_in_tmux.sh"
        cp "$REPO_DIR/scripts/launch_ros_in_tmux.sh" /usr/local/bin/
        chmod +x /usr/local/bin/launch_ros_in_tmux.sh
    fi
fi

# 3. Update udev rules if present
log "Checking udev rules..."
if [ -d "$REPO_DIR/udev" ]; then
    for rule_file in "$REPO_DIR/udev"/*.rules; do
        if [ -f "$rule_file" ]; then
            rule_name=$(basename "$rule_file")
            log "Copying $rule_name to /etc/udev/rules.d/"
            cp "$rule_file" /etc/udev/rules.d/
        fi
    done
    udevadm control --reload-rules
    udevadm trigger
    log "Udev rules reloaded"
fi

# 4. Update Bluetooth configurations
log "Checking Bluetooth configurations..."
if [ -f "$REPO_DIR/config/bluetooth/main.conf" ]; then
    log "Updating /etc/bluetooth/main.conf"
    cp "$REPO_DIR/config/bluetooth/main.conf" /etc/bluetooth/main.conf
fi

if [ -f "$REPO_DIR/config/bluetooth/nv-bluetooth-service.conf" ]; then
    log "Updating bluetooth service override..."
    mkdir -p /lib/systemd/system/bluetooth.service.d/
    cp "$REPO_DIR/config/bluetooth/nv-bluetooth-service.conf" /lib/systemd/system/bluetooth.service.d/nv-bluetooth-service.conf
    systemctl daemon-reload
    log "Systemd daemon reloaded after bluetooth override"
fi

# 5. Rebuild ROS2 workspace if needed
log "Checking ROS2 workspace..."
if [ -d "$REPO_DIR/ros2_ws/src" ]; then
    log "Rebuilding ROS2 workspace..."
    cd "$REPO_DIR/ros2_ws"
    
    # Run as the actual user, not root
    ACTUAL_USER=${SUDO_USER:-$USER}
    sudo -u "$ACTUAL_USER" bash -c "cd $REPO_DIR/ros2_ws && source /opt/ros/humble/setup.bash && rm -rf build/ install/ log/ && colcon build"
    
    if [ $? -eq 0 ]; then
        log "ROS2 workspace rebuilt successfully"
    else
        log "ERROR: Failed to rebuild ROS2 workspace"
        exit 1
    fi
fi

# 6. Install/update Python dependencies if requirements.txt changed
log "Checking Python dependencies..."
if [ -f "$REPO_DIR/requirements.txt" ]; then
    log "Updating Python dependencies..."
    pip3 install -r "$REPO_DIR/requirements.txt" --upgrade
fi

# 7. Restart relevant services
log "Restarting services..."
SERVICES_TO_RESTART=("bluetooth.service" "discovery-server.service" "ros-app.service")

for service in "${SERVICES_TO_RESTART[@]}"; do
    log "Enabling and restarting $service"
    systemctl enable "$service"
    systemctl restart "$service"
done

# 8. Launch ROS nodes in Tmux
log "Launching ROS nodes in tmux..."
ACTUAL_USER=${SUDO_USER:-$USER}
# Run as the actual user in the background
sudo -u "$ACTUAL_USER" INNATE_OS_ROOT="$REPO_DIR" zsh "$REPO_DIR/scripts/launch_ros_in_tmux.sh" &

# 9. Optional: Restart Docker containers if using them
# if command -v docker-compose &> /dev/null; then
#     log "Restarting Docker containers..."
#     cd "$REPO_DIR"
#     docker-compose -f docker-compose.prod.yml down
#     docker-compose -f docker-compose.prod.yml up -d
# fi

log "========================================"
log "Post-update script completed successfully"
log "========================================"

# Fix log file ownership
ensure_log_ownership

exit 0

