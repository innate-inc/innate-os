#!/bin/bash
# Post-Update Script for Innate-OS
# This script runs after update/install to configure system components
# Requires root privileges via sudo
#
# Usage: sudo ./post_update.sh [--first-install]
#   --first-install  Skip dependency installation (used on fresh install, already done by install.sh)
#
# This script handles:
#   1. Systemd service files
#   2. Helper scripts in /usr/local/bin
#   3. Udev rules
#   4. Bluetooth configuration
#   4b. Arducam microphone setup (ALSA + PulseAudio)
#   5. Apt/pip dependencies (skipped on first-install)
#   6. Rebuild ROS2 workspace
#   7. Zsh configuration
#   8. DDS setup
#   9. Sudoers configuration
#   10. Service restart

set -e  # Exit on error

# Parse arguments
FIRST_INSTALL=false
for arg in "$@"; do
    case $arg in
        --first-install)
            FIRST_INSTALL=true
            shift
            ;;
    esac
done

# Check for root privileges
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root. Please use sudo." >&2
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
LOG_FILE="$REPO_DIR/logs/post_update.log"

# Get the actual user (not root)
ACTUAL_USER=${SUDO_USER:-$USER}
ACTUAL_HOME=$(eval echo ~$ACTUAL_USER)

# Create logs directory if it doesn't exist
mkdir -p "$(dirname "$LOG_FILE")"

# Logging function
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Ensure log file has correct ownership
ensure_log_ownership() {
    if [ -f "$LOG_FILE" ]; then
        chown "$ACTUAL_USER:$ACTUAL_USER" "$LOG_FILE" 2>/dev/null || true
    fi
}

log "========================================"
if [ "$FIRST_INSTALL" = true ]; then
    log "Starting post-install script (first install mode)"
else
    log "Starting post-update script"
fi
log "Repository: $REPO_DIR"
log "User: $ACTUAL_USER"
log "========================================"

# Check hardware revision - R5 is no longer supported for updates beyond 0.1.0
R5_MAX_VERSION="0.1.1"
check_hardware_revision() {
    local robot_info_file="$REPO_DIR/data/robot_info.json"
    
    if [ -f "$robot_info_file" ]; then
        # Extract hardware_revision using grep and sed (no jq dependency)
        local hw_rev=$(grep -o '"hardware_revision"[[:space:]]*:[[:space:]]*"[^"]*"' "$robot_info_file" | sed 's/.*: *"\([^"]*\)".*/\1/')
        
        if [ "$hw_rev" = "R5" ]; then
            log "WARNING: Hardware revision R5 detected"
            log "R5 robots are limited to version $R5_MAX_VERSION"
            log "Reverting to $R5_MAX_VERSION..."
            
            # Checkout the max supported version for R5
            if git -C "$REPO_DIR" checkout "$R5_MAX_VERSION" 2>/dev/null; then
                log "Successfully reverted to $R5_MAX_VERSION"
            else
                log "ERROR: Failed to checkout $R5_MAX_VERSION"
            fi
            
            echo ""
            echo "╔════════════════════════════════════════════════════════════╗"
            echo "║  HARDWARE REVISION R5 - UPDATE NOT SUPPORTED               ║"
            echo "╠════════════════════════════════════════════════════════════╣"
            echo "║  This robot (R5) cannot receive updates beyond v$R5_MAX_VERSION.   ║"
            echo "║  The system has been kept at the last compatible version.  ║"
            echo "║  Please contact support for hardware upgrade options.      ║"
            echo "╚════════════════════════════════════════════════════════════╝"
            echo ""
            
            exit 0
        fi
    fi
}

check_hardware_revision

# Configure git for data integrity on unexpected shutdowns (e.g., power loss).
# core.fsync ensures git flushes writes to disk, preventing repository corruption.
# See: https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsync
log "Configuring git fsync settings..."
sudo -u "$ACTUAL_USER" git config --global core.fsync added,reference
sudo -u "$ACTUAL_USER" git config --global core.fsyncMethod fsync
log "Git fsync settings configured"

# Stop running services before updating (keep app.cpp alive during build)
log "Stopping services to begin update..."

# Stop systemd services first (if they exist)
for service in zenoh-router.service ros-app.service ble-provisioner.service; do
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        log "Stopping $service"
        systemctl stop "$service"
    fi
done

# Gracefully stop tmux panes - keep app.cpp alive during build
if sudo -u "$ACTUAL_USER" tmux has-session -t ros_nodes 2>/dev/null; then
    log "Stopping ROS nodes (keeping app.cpp alive during build)..."
    
    # Kill all windows except app-bringup
    for window in "arm-recorder" "brain-nav" "behaviors-inputs" "stream" "ik-logger"; do
        if sudo -u "$ACTUAL_USER" tmux list-windows -t ros_nodes 2>/dev/null | grep -q "$window"; then
            log "  Stopping window: $window"
            sudo -u "$ACTUAL_USER" tmux kill-window -t "ros_nodes:$window" 2>/dev/null || true
        fi
    done
    
    log "Other nodes stopped. App and bringup still running for status updates."
else
    log "No ros_nodes tmux session found"
fi

log "Services stopped (app.cpp still running)."

# -----------------------------------------------------------------------------
# 1. Update systemd service files
# -----------------------------------------------------------------------------
log "Installing systemd service files..."
if [ -d "$REPO_DIR/systemd" ]; then
    for service_file in "$REPO_DIR/systemd"/*.service; do
        if [ -f "$service_file" ]; then
            service_name=$(basename "$service_file")
            log "  Installing $service_name"

            # Update User= directive in service files to match actual user
            # Remove any existing symlink to avoid truncating source file
            rm -f /etc/systemd/system/"$service_name"
            sed -e "s/User=jetson1/User=$ACTUAL_USER/g" \
                -e "s|/home/jetson1|$ACTUAL_HOME|g" \
                "$service_file" > /etc/systemd/system/"$service_name"
        fi
    done
    systemctl daemon-reload
    log "Systemd daemon reloaded"
fi

# -----------------------------------------------------------------------------
# 2. Update helper scripts in /usr/local/bin
# -----------------------------------------------------------------------------
log "Installing helper scripts..."
if [ -d "$REPO_DIR/scripts" ]; then
    # Symlink innate-update (so git pull automatically updates the command)
    if [ -f "$REPO_DIR/scripts/update/innate-update" ]; then
        log "  Symlinking innate-update"
        rm -f /usr/local/bin/innate-update
        ln -s "$REPO_DIR/scripts/update/innate-update" /usr/local/bin/innate-update
    fi

    # Symlink restart script if it exists
    if [ -f "$REPO_DIR/scripts/restart_robot_networking.sh" ]; then
        log "  Symlinking restart_robot_networking.sh"
        rm -f /usr/local/bin/restart_robot_networking.sh
        ln -s "$REPO_DIR/scripts/restart_robot_networking.sh" /usr/local/bin/restart_robot_networking.sh
    fi

    # Symlink tmux launcher if it exists
    if [ -f "$REPO_DIR/scripts/launch_ros_in_tmux.sh" ]; then
        log "  Symlinking launch_ros_in_tmux.sh"
        rm -f /usr/local/bin/launch_ros_in_tmux.sh
        ln -s "$REPO_DIR/scripts/launch_ros_in_tmux.sh" /usr/local/bin/launch_ros_in_tmux.sh
    fi

    # Copy zsh history fixer if it exists
    if [ -f "$REPO_DIR/scripts/fix-zsh-history.sh" ]; then
        log "  Installing fix-zsh-history.sh"
        cp "$REPO_DIR/scripts/fix-zsh-history.sh" /usr/local/bin/
        chmod +x /usr/local/bin/fix-zsh-history.sh
    fi
fi

# -----------------------------------------------------------------------------
# 3. Update udev rules
# -----------------------------------------------------------------------------
log "Installing udev rules..."
if [ -d "$REPO_DIR/udev" ]; then
    for rule_file in "$REPO_DIR/udev"/*.rules; do
        if [ -f "$rule_file" ]; then
            rule_name=$(basename "$rule_file")
            log "  Installing $rule_name"
            rm -f /etc/udev/rules.d/"$rule_name"
            cp "$rule_file" /etc/udev/rules.d/
        fi
    done
    udevadm control --reload-rules
    udevadm trigger
    log "Udev rules reloaded"
fi

# -----------------------------------------------------------------------------
# 4. Configure Hardware (Bluetooth, Arducam, WiFi power management)
# -----------------------------------------------------------------------------
log "Configuring hardware..."

HARDWARE_REBOOT_REQUIRED=false
HARDWARE_SCRIPT="$REPO_DIR/scripts/update/configure_hardware.sh"
if [ -f "$HARDWARE_SCRIPT" ]; then
    chmod +x "$HARDWARE_SCRIPT"
    "$HARDWARE_SCRIPT" "$REPO_DIR" 2>&1 | tee -a "$LOG_FILE"
    HARDWARE_EXIT_CODE=${PIPESTATUS[0]}
    if [ $HARDWARE_EXIT_CODE -eq 0 ]; then
        log "  Hardware configuration completed successfully"
    elif [ $HARDWARE_EXIT_CODE -eq 2 ]; then
        log "  Hardware configuration completed (reboot required)"
        HARDWARE_REBOOT_REQUIRED=true
    else
        log "  WARNING: Hardware configuration script failed"
    fi
else
    log "  WARNING: configure_hardware.sh not found at $HARDWARE_SCRIPT"
fi

# -----------------------------------------------------------------------------
# 5. Install/update dependencies (skip on first install)
# -----------------------------------------------------------------------------
if [ "$FIRST_INSTALL" = true ]; then
    log "Skipping dependencies (already installed during first install)"
else
    # Apt dependencies
    log "Checking apt dependencies..."
    APT_DEPS_FILE="$REPO_DIR/ros2_ws/apt-dependencies.txt"
    if [ -f "$APT_DEPS_FILE" ]; then
        # Add git-core PPA for latest git version (if not already added)
        if ! grep -q "git-core/ppa" /etc/apt/sources.list.d/*.list 2>/dev/null; then
            log "  Adding git-core PPA..."
            add-apt-repository -y ppa:git-core/ppa
        fi
        log "  Installing apt dependencies..."
        apt-get update
        grep -v '^#' "$APT_DEPS_FILE" | grep -v '^$' | xargs apt-get install -y
        log "  Apt dependencies installed"
    fi

    # Pip dependencies
    log "Checking Python dependencies..."
    PIP_DEPS_FILE="$REPO_DIR/ros2_ws/pip-requirements.txt"
    if [ -f "$PIP_DEPS_FILE" ]; then
        log "  Installing pip dependencies..."
        sudo -u "$ACTUAL_USER" pip3 install -r "$PIP_DEPS_FILE" --upgrade
        log "  Pip dependencies installed"
    fi
fi

# -----------------------------------------------------------------------------
# 6. Rebuild ROS2 workspace if needed
# -----------------------------------------------------------------------------
log "Checking ROS2 workspace..."
if [ -d "$REPO_DIR/ros2_ws/src" ]; then
    log "Rebuilding ROS2 workspace..."
    cd "$REPO_DIR/ros2_ws"

    # Run as the actual user, not root
    sudo -u "$ACTUAL_USER" bash -c "cd $REPO_DIR/ros2_ws && source /opt/ros/humble/setup.bash && rm -rf build/ install/ log/ && colcon build"

    if [ $? -eq 0 ]; then
        log "ROS2 workspace rebuilt successfully"
    else
        log "ERROR: Failed to rebuild ROS2 workspace"
        exit 1
    fi
fi

# Now kill the remaining app.cpp pane (build is done, safe to stop it)
if sudo -u "$ACTUAL_USER" tmux has-session -t ros_nodes 2>/dev/null; then
    log "Build complete. Stopping app.cpp..."
    sudo -u "$ACTUAL_USER" tmux kill-session -t ros_nodes 2>/dev/null || true
fi

# -----------------------------------------------------------------------------
# 7. Setup Zsh configuration
# -----------------------------------------------------------------------------
log "Setting up Zsh configuration..."

# Install oh-my-zsh if not present
if [ ! -d "$ACTUAL_HOME/.oh-my-zsh" ]; then
    log "  Installing Oh My Zsh..."
    sudo -u "$ACTUAL_USER" sh -c 'RUNZSH=no CHSH=no sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"' || true
fi

# Backup existing .zshrc if it exists and isn't ours
if [ -f "$ACTUAL_HOME/.zshrc" ]; then
    if ! grep -q "INNATE_OS_ROOT" "$ACTUAL_HOME/.zshrc"; then
        log "  Backing up existing .zshrc"
        cp "$ACTUAL_HOME/.zshrc" "$ACTUAL_HOME/.zshrc.backup.$(date +%s)"
    fi
fi

# Copy our zsh configuration files
if [ -d "$REPO_DIR/zshrcs" ]; then
    log "  Installing Innate zsh configuration"

    # Copy .zshrc.pre-oh-my-zsh (contains ROS2/DDS setup)
    if [ -f "$REPO_DIR/zshrcs/.zshrc.pre-oh-my-zsh" ]; then
        # Update paths in the file
        sed -e "s|/home/jetson1|$ACTUAL_HOME|g" \
            "$REPO_DIR/zshrcs/.zshrc.pre-oh-my-zsh" > "$ACTUAL_HOME/.zshrc.pre-oh-my-zsh"
        chown "$ACTUAL_USER:$ACTUAL_USER" "$ACTUAL_HOME/.zshrc.pre-oh-my-zsh"
    fi

    # Copy main .zshrc
    if [ -f "$REPO_DIR/zshrcs/.zshrc" ]; then
        # Update paths in the file
        sed -e "s|/home/jetson1|$ACTUAL_HOME|g" \
            "$REPO_DIR/zshrcs/.zshrc" > "$ACTUAL_HOME/.zshrc"
        chown "$ACTUAL_USER:$ACTUAL_USER" "$ACTUAL_HOME/.zshrc"
    fi
fi

# Ensure INNATE_OS_ROOT is set correctly in .zshrc
if [ -f "$ACTUAL_HOME/.zshrc" ]; then
    if ! grep -q "export INNATE_OS_ROOT=" "$ACTUAL_HOME/.zshrc"; then
        log "  Adding INNATE_OS_ROOT to .zshrc"
        echo "" >> "$ACTUAL_HOME/.zshrc"
        echo "export INNATE_OS_ROOT=\"$REPO_DIR\"" >> "$ACTUAL_HOME/.zshrc"
    else
        # Update existing INNATE_OS_ROOT
        sed -i "s|export INNATE_OS_ROOT=.*|export INNATE_OS_ROOT=\"$REPO_DIR\"|g" "$ACTUAL_HOME/.zshrc"
    fi
fi

# Set zsh as default shell if not already
if [ "$(getent passwd $ACTUAL_USER | cut -d: -f7)" != "/bin/zsh" ] && [ -x /bin/zsh ]; then
    log "  Setting zsh as default shell for $ACTUAL_USER"
    chsh -s /bin/zsh "$ACTUAL_USER" || true
fi

# -----------------------------------------------------------------------------
# 8. Setup DDS configuration
# -----------------------------------------------------------------------------
log "Setting up DDS configuration..."
if [ -d "$REPO_DIR/dds" ]; then
    # Ensure DDS scripts are executable
    chmod +x "$REPO_DIR/dds"/*.zsh 2>/dev/null || true

    # Generate initial DDS config (will be regenerated on shell login)
    if [ -f "$REPO_DIR/dds/setup_dds.zsh" ]; then
        log "  DDS setup script ready at $REPO_DIR/dds/setup_dds.zsh"
    fi
fi

# -----------------------------------------------------------------------------
# 9. Configure sudoers for passwordless restart script
# -----------------------------------------------------------------------------
log "Configuring sudoers..."

# Allow user to run restart_robot_networking.sh without password
SUDOERS_FILE="/etc/sudoers.d/innate-os"
cat > "$SUDOERS_FILE" << EOF
# Innate-OS sudoers configuration
# Allow $ACTUAL_USER to run specific scripts without password

# Restart robot networking (called by BLE provisioner)
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/local/bin/restart_robot_networking.sh

# Post-update script (called by innate-update)
$ACTUAL_USER ALL=(ALL) NOPASSWD: $REPO_DIR/scripts/update/post_update.sh
EOF
chmod 440 "$SUDOERS_FILE"
log "  Sudoers configured for $ACTUAL_USER"

# -----------------------------------------------------------------------------
# 10. Enable and restart services
# -----------------------------------------------------------------------------
log "Enabling and starting services..."

# List of services to enable/start
SERVICES=("zenoh-router.service" "ros-app.service")

# Add ble-provisioner if the service file exists
if [ -f "/etc/systemd/system/ble-provisioner.service" ]; then
    SERVICES+=("ble-provisioner.service")
fi

# Add arducam-audio if the service file exists
if [ -f "/etc/systemd/system/arducam-audio.service" ]; then
    SERVICES+=("arducam-audio.service")
fi

# Add bluetooth if available
if systemctl list-unit-files bluetooth.service &>/dev/null; then
    SERVICES+=("bluetooth.service")
fi

for service in "${SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/$service" ] || systemctl list-unit-files "$service" &>/dev/null; then
        log "  Enabling $service"
        systemctl enable "$service" 2>/dev/null || true
        log "  Starting $service"
        systemctl start "$service" 2>/dev/null || true
    fi
done

# -----------------------------------------------------------------------------
# 11. Launch ROS nodes in Tmux (optional, depends on ros-app.service)
# -----------------------------------------------------------------------------
# Note: If ros-app.service is configured correctly, it will launch the tmux session
# If not using systemd, you can manually launch:
# sudo -u "$ACTUAL_USER" INNATE_OS_ROOT="$REPO_DIR" bash "$REPO_DIR/scripts/launch_ros_in_tmux.sh" &

log "========================================"
log "Post-update script completed successfully"
log "========================================"
log ""
log "To start ROS manually:"
log "  tmux attach -t ros_nodes  (if already running)"
log "  OR"
log "  launch_ros_in_tmux.sh     (to start fresh)"
log ""

# Notify if reboot is required for hardware changes
if [ "$HARDWARE_REBOOT_REQUIRED" = true ]; then
    log ""
    log "╔════════════════════════════════════════════════════════════╗"
    log "║  REBOOT REQUIRED                                           ║"
    log "║  Hardware configuration changes require a system reboot.   ║"
    log "╚════════════════════════════════════════════════════════════╝"
    log ""
fi

# Fix log file ownership
ensure_log_ownership

exit 0


