#!/bin/bash
# Post-Update Script for Innate-OS
# This script runs after git pull to update system components
# Requires root privileges via sudo

set -e  # Exit on error

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
LOG_FILE="$REPO_DIR/logs/post_update.log"

# Create logs directory if it doesn't exist
mkdir -p "$(dirname "$LOG_FILE")"

# Logging function
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "========================================"
log "Starting post-update script"
log "Repository: $REPO_DIR"
log "========================================"

# Make sure services are stopped before updating
log "Ensuring services are stopped..."
for service in discovery-server.service ros-app.service; do
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        log "Stopping $service"
        systemctl stop "$service"
    fi
done

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

# 4. Rebuild ROS2 workspace if needed
log "Checking ROS2 workspace..."
if [ -d "$REPO_DIR/ros2_ws/src" ]; then
    log "Rebuilding ROS2 workspace..."
    cd "$REPO_DIR/ros2_ws"
    
    # Run as the actual user, not root
    ACTUAL_USER=${SUDO_USER:-jetson1}
    sudo -u "$ACTUAL_USER" bash -c "cd $REPO_DIR/ros2_ws && source /opt/ros/humble/setup.bash && rm -rf build/ install/ log/ && colcon build"
    
    if [ $? -eq 0 ]; then
        log "ROS2 workspace rebuilt successfully"
    else
        log "ERROR: Failed to rebuild ROS2 workspace"
        exit 1
    fi
fi

# 5. Install/update Python dependencies if requirements.txt changed
log "Checking Python dependencies..."
if [ -f "$REPO_DIR/requirements.txt" ]; then
    log "Updating Python dependencies..."
    pip3 install -r "$REPO_DIR/requirements.txt" --upgrade
fi

# 6. Restart relevant services
log "Restarting services..."
SERVICES_TO_RESTART=("discovery-server.service" "ros-app.service")

for service in "${SERVICES_TO_RESTART[@]}"; do
    if systemctl is-active --quiet "$service"; then
        log "Restarting $service"
        systemctl restart "$service"
    else
        log "Service $service is not active, skipping restart"
    fi
done

# 7. Optional: Restart Docker containers if using them
# if command -v docker-compose &> /dev/null; then
#     log "Restarting Docker containers..."
#     cd "$REPO_DIR"
#     docker-compose -f docker-compose.prod.yml down
#     docker-compose -f docker-compose.prod.yml up -d
# fi

log "========================================"
log "Post-update script completed successfully"
log "========================================"

# Send notification (optional - requires notification system)
# echo "Innate-OS update completed on $(hostname)" | mail -s "Update Notification" admin@example.com

exit 0

