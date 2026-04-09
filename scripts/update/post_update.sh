#!/bin/bash
# Post-Update Script for Innate-OS
# This script runs after update/install to configure system components
# Requires root privileges via sudo
#
# Usage: sudo ./post_update.sh
#
# This script handles:
#   1. Systemd service files
#   2. Helper scripts in /usr/local/bin
#   3. Udev rules
#   4. Bluetooth configuration
#   4b. Arducam microphone setup (ALSA)
#   5. Apt/pip dependencies
#   6. Rebuild ROS2 workspace
#   7. Zsh configuration
#   8. DDS setup
#   9. Sudoers configuration
#   10. Move primitives .h5 files to skills directory
#   11. Service restart

set -e  # Exit on error

# Print a warning banner on non-zero exit
SCRIPT_PATH=""
if command -v realpath >/dev/null 2>&1; then
    SCRIPT_PATH="$(realpath "$0")"
else
    SCRIPT_PATH="$0"
fi


# Parse arguments
SKIP_ROS_CLEAN=false
for arg in "$@"; do
    case $arg in
        --skip-ros-clean)
            SKIP_ROS_CLEAN=true
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

log_success() {
    local ts
    ts="$(date +'%Y-%m-%d %H:%M:%S')"
    echo -e "[$ts] \033[1;32m$*\033[0m"
    echo "[$ts] $*" >> "$LOG_FILE"
}

log_error() {
    local ts
    ts="$(date +'%Y-%m-%d %H:%M:%S')"
    echo -e "[$ts] \033[1;31m$*\033[0m"
    echo "[$ts] $*" >> "$LOG_FILE"
}

on_exit() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        log_error "============================================================"
        log_error "============================================================"
        log_error "UPDATE FAILED / PARTIALLY COMPLETED"
        log_error "Please re-run: sudo $SCRIPT_PATH"
        log_error "Or contact support for help."
        log_error "============================================================"
        log_error "============================================================"
        echo ""
    fi
}
trap on_exit EXIT

# Ensure log file has correct ownership
ensure_log_ownership() {
    if [ -f "$LOG_FILE" ]; then
        chown "$ACTUAL_USER:$ACTUAL_USER" "$LOG_FILE" 2>/dev/null || true
    fi
}

log "========================================"
log "Starting post-update script"
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

# Migrate git remote from old release repo to main repo
CURRENT_REMOTE=$(sudo -u "$ACTUAL_USER" git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)
if [ "$CURRENT_REMOTE" = "git@github.com:innate-inc/innate-os-release.git" ]; then
    log "Migrating git remote from innate-os-release to innate-os..."
    sudo -u "$ACTUAL_USER" git -C "$REPO_DIR" remote set-url origin https://github.com/innate-inc/innate-os.git
    log "Git remote updated to https://github.com/innate-inc/innate-os.git"
fi

# -----------------------------------------------------------------------------
# 0. Migrate .env file (comment out deprecated URLs)
# -----------------------------------------------------------------------------
ENV_FILE="$REPO_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    NEEDS_ENV_MIGRATION=false

    # Check for old/deprecated URL values
    if grep -q '^INNATE_PROXY_URL=https://robot-services\.innate\.bot' "$ENV_FILE" || \
       grep -q '^BRAIN_WEBSOCKET_URI=wss://brain\.innate\.bot' "$ENV_FILE" || \
       grep -q '^TELEMETRY_URL=https://logs\.innate\.bot' "$ENV_FILE"; then
        NEEDS_ENV_MIGRATION=true
    fi

    if [ "$NEEDS_ENV_MIGRATION" = true ]; then
        log "Migrating .env file (commenting out deprecated URLs)..."

        # Backup .env
        cp "$ENV_FILE" "$ACTUAL_HOME/.env.backup"
        chown "$ACTUAL_USER:$ACTUAL_USER" "$ACTUAL_HOME/.env.backup"
        log "  Backed up .env to $ACTUAL_HOME/.env.backup"

        # Comment out deprecated values
        sed -i 's|^INNATE_PROXY_URL=https://robot-services\.innate\.bot|# INNATE_PROXY_URL=https://robot-services.innate.bot|' "$ENV_FILE"
        sed -i 's|^BRAIN_WEBSOCKET_URI=wss://brain\.innate\.bot|# BRAIN_WEBSOCKET_URI=wss://brain.innate.bot|' "$ENV_FILE"
        sed -i 's|^TELEMETRY_URL=https://logs\.innate\.bot|# TELEMETRY_URL=https://logs.innate.bot|' "$ENV_FILE"

        # Add reference comment at the bottom of .env
        if ! grep -q 'Refer to .env.template for examples' "$ENV_FILE"; then
            echo "" >> "$ENV_FILE"
            echo "# Refer to .env.template for examples" >> "$ENV_FILE"
        fi

        chown "$ACTUAL_USER:$ACTUAL_USER" "$ENV_FILE"
        log "  Deprecated URLs commented out in .env"
        log "  Variables unset from environment"
    fi
fi

# Stop running services before updating (keep app.cpp alive during build)
log "Stopping services to begin update..."

# Stop systemd services first (if they exist)
for service in jetson-perf.service zenoh-router.service ros-app.service ble-provisioner.service speaker-keepalive.service; do
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

    # Symlink innate dev CLI
    if [ -f "$REPO_DIR/scripts/innate" ]; then
        log "  Symlinking innate"
        rm -f /usr/local/bin/innate
        ln -s "$REPO_DIR/scripts/innate" /usr/local/bin/innate
    fi

    # Regenerate zsh completions from the Click command tree
    if [ -f "$REPO_DIR/scripts/innate" ]; then
        log "  Generating zsh completions"
        # Clean up stale completions from old path (was root-owned, blocks colcon rebuild)
        rm -rf "$REPO_DIR/ros2_ws/build/completions"
        mkdir -p "$REPO_DIR/scripts/build/completions"
        chown -R "$ACTUAL_USER:$ACTUAL_USER" "$REPO_DIR/scripts/build"
        sudo -u "$ACTUAL_USER" "$REPO_DIR/scripts/innate" completions > "$REPO_DIR/scripts/build/completions/_innate"
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



# 4. Configure passwordless shutdown for ROS app
log "Configuring passwordless shutdown..."
ACTUAL_USER=${SUDO_USER:-$USER}
SUDOERS_FILE="/etc/sudoers.d/ros-shutdown"
echo "$ACTUAL_USER ALL=(ALL) NOPASSWD: /sbin/shutdown" > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
log "Passwordless shutdown configured for user $ACTUAL_USER"


# -----------------------------------------------------------------------------
# 5. Update Bluetooth configurations (optional - only on systems with bluetooth)
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
    elif [ $HARDWARE_EXIT_CODE -eq 3 ]; then
        log "  Hardware configuration completed (reboot required)"
        HARDWARE_REBOOT_REQUIRED=true
    else
        log "  WARNING: Hardware configuration script failed"
    fi
else
    log "  WARNING: configure_hardware.sh not found at $HARDWARE_SCRIPT"
fi

# -----------------------------------------------------------------------------
# 6. Install/update dependencies
# -----------------------------------------------------------------------------
    # Apt dependencies
    log "Checking apt dependencies..."
    
    # Check if ROS 2 Humble is installed
    if ! dpkg -l | grep -q "^ii.*ros-humble-ros-base"; then
        log "  ROS 2 Humble not found. Installing ROS 2 Humble base..."
        
        # Add ROS2 GPG key
        if [ ! -f /usr/share/keyrings/ros-archive-keyring.gpg ]; then
            log "    Adding ROS 2 GPG key..."
            curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
                -o /usr/share/keyrings/ros-archive-keyring.gpg || {
                log "    ERROR: Failed to download ROS 2 GPG key"
                exit 1
            }
        fi
        
        # Add ROS2 repository
        if [ ! -f /etc/apt/sources.list.d/ros2.list ]; then
            log "    Adding ROS 2 repository..."
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" | \
                tee /etc/apt/sources.list.d/ros2.list > /dev/null
        fi
        
        # Install ROS2 base
        log "    Updating package lists..."
        apt-get update || {
            log "    ERROR: Failed to update package lists"
            exit 1
        }
        log "    Installing ROS 2 Humble base..."
        apt-get -o DPkg::Lock::Timeout=45 install -y ros-humble-ros-base || {
            log "    ERROR: Failed to install ROS 2 Humble base"
            exit 1
        }
        log "  ROS 2 Humble base installed"
    else
        log "  ROS 2 Humble base already installed"
    fi
    
    # Check if Innate packages repository is configured
    if [ ! -f /usr/share/keyrings/innate-archive-keyring.gpg ] || [ ! -f /etc/apt/sources.list.d/innate.list ]; then
        log "  Adding Innate packages repository..."
        
        # Add Innate GPG key
        if [ ! -f /usr/share/keyrings/innate-archive-keyring.gpg ]; then
            log "    Adding Innate GPG key..."
            curl -fsSL https://innate-inc.github.io/innate-packages/pubkey.gpg | \
                gpg --dearmor -o /usr/share/keyrings/innate-archive-keyring.gpg || {
                log "    ERROR: Failed to download Innate GPG key"
                exit 1
            }
        fi
        
        # Add Innate repository
        if [ ! -f /etc/apt/sources.list.d/innate.list ]; then
            log "    Adding Innate repository..."
            echo "deb [signed-by=/usr/share/keyrings/innate-archive-keyring.gpg] https://innate-inc.github.io/innate-packages/ $(lsb_release -cs) main" | \
                tee /etc/apt/sources.list.d/innate.list > /dev/null
        fi
        
        log "  Innate packages repository configured"
    else
        log "  Innate packages repository already configured"
    fi
    
    APT_DEPS_COMMON="$REPO_DIR/ros2_ws/apt-dependencies.common.txt"
    APT_DEPS_HARDWARE="$REPO_DIR/ros2_ws/apt-dependencies.hardware.txt"

    # Add git-core PPA for latest git version (if not already added)
    if ! grep -q "git-core/ppa" /etc/apt/sources.list.d/*.list 2>/dev/null; then
        log "  Adding git-core PPA..."
        add-apt-repository -y ppa:git-core/ppa
    fi

    apt-get update

    # Install all apt dependencies (common + hardware-specific) in one go
    if [ -f "$APT_DEPS_COMMON" ] && [ -f "$APT_DEPS_HARDWARE" ]; then
        log "  Installing apt dependencies (common + hardware)..."
        cat "$APT_DEPS_COMMON" "$APT_DEPS_HARDWARE" | grep -v '^#' | grep -v '^$' | xargs apt-get -o DPkg::Lock::Timeout=45 install -y
        log "  Apt dependencies installed"
    fi

    # Remove PulseAudio (conflicts with ALSA-only audio setup)
    if dpkg -l | grep -q "^ii.*pulseaudio "; then
        log "Removing PulseAudio..."
        apt-get purge -y pulseaudio pulseaudio-utils 2>/dev/null || true
        apt-get autoremove -y 2>/dev/null || true
        log "  PulseAudio removed"
    else
        log "  PulseAudio not installed, skipping removal"
    fi

    # Pip dependencies
    log "Checking Python dependencies..."
    PIP_DEPS_FILE="$REPO_DIR/ros2_ws/pip-requirements.txt"
    if [ -f "$PIP_DEPS_FILE" ]; then
        # Uninstall mediapipe to avoid protobuf dependency conflict
        # mediapipe requires protobuf<5, but we need protobuf>=6 for other packages
        if sudo -u "$ACTUAL_USER" pip3 show mediapipe &>/dev/null; then
            log "  Uninstalling mediapipe (protobuf conflict)..."
            sudo -u "$ACTUAL_USER" pip3 uninstall -y mediapipe 2>/dev/null || true
            log "  Mediapipe uninstalled"
        else
            log "  Mediapipe not installed, skipping uninstall"
        fi
        
        # Install Jetson-optimized PyTorch separately with ONLY the Jetson index
        # pip prefers manylinux wheels over platform-specific wheels, so we CANNOT
        # have PyPI as a fallback when installing torch or it will grab the CPU wheel
        JETSON_INDEX="https://pypi.jetson-ai-lab.io/jp6/cu126"
        TORCH_VERSION="2.8.0"
        TORCHVISION_VERSION="0.23.0"
        
        CUDA_AVAILABLE=$(sudo -u "$ACTUAL_USER" python3 -c "import torch; print(torch.cuda.is_available())" 2>/dev/null || echo "False")
        if [[ "$CUDA_AVAILABLE" != "True" ]]; then
            log "  PyTorch CUDA not available, reinstalling from Jetson AI Lab..."
            sudo -u "$ACTUAL_USER" pip3 uninstall -y torch torchvision torchaudio 2>/dev/null || true
            sudo -u "$ACTUAL_USER" pip3 install --no-cache-dir \
                "torch==$TORCH_VERSION" \
                "torchvision==$TORCHVISION_VERSION" \
                torchaudio \
                --index-url "$JETSON_INDEX"
            log "  PyTorch installed from Jetson AI Lab"
        fi
        
        # Install remaining pip dependencies (torch already installed above)
        log "  Installing other pip dependencies..."
        sudo -u "$ACTUAL_USER" pip3 install -r "$PIP_DEPS_FILE" --upgrade-strategy only-if-needed
        log "  Pip dependencies installed"
    fi

# -----------------------------------------------------------------------------
# 6. Rebuild ROS2 workspace if needed
# -----------------------------------------------------------------------------
log "Checking ROS2 workspace..."
if [ -d "$REPO_DIR/ros2_ws/src" ]; then
    log "Rebuilding ROS2 workspace..."
    cd "$REPO_DIR/ros2_ws"

    # Run as the actual user, not root
    if [ "$SKIP_ROS_CLEAN" = true ]; then
        log "  Skipping clean build (--skip-ros-clean)"
        sudo -u "$ACTUAL_USER" bash -c "cd $REPO_DIR/ros2_ws && source /opt/ros/humble/setup.bash && colcon build"
    else
        sudo -u "$ACTUAL_USER" bash -c "cd $REPO_DIR/ros2_ws && source /opt/ros/humble/setup.bash && rm -rf build/ install/ log/ && colcon build"
    fi

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
# 8. Setup Zsh configuration
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

# Add user to dialout and i2c groups for serial port and I2C access
if ! groups "$ACTUAL_USER" | grep -q "\bdialout\b" || ! groups "$ACTUAL_USER" | grep -q "\bi2c\b"; then
    log "  Adding $ACTUAL_USER to dialout and i2c groups for serial port and I2C access"
    usermod -aG dialout,i2c "$ACTUAL_USER" || true
    log "  Note: User may need to log out and back in for group changes to take effect"
fi

# Suppress default Ubuntu MOTD on SSH login (our custom banner in .zshrc is enough)
HUSHLOGIN="$ACTUAL_HOME/.hushlogin"
if [ ! -f "$HUSHLOGIN" ]; then
    log "  Creating .hushlogin to suppress Ubuntu MOTD"
    touch "$HUSHLOGIN"
    chown "$ACTUAL_USER:$ACTUAL_USER" "$HUSHLOGIN"
fi

# -----------------------------------------------------------------------------
# 9. Setup DDS configuration
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
# 10. Configure sudoers for passwordless restart script
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

# NetworkManager CLI (called by BLE provisioner for WiFi configuration)
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli
$ACTUAL_USER ALL=(ALL) NOPASSWD: /bin/nmcli

# systemctl for ROS node management (called by innate CLI)
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ros-app.service
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop ros-app.service
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ros-app.service
EOF
chmod 440 "$SUDOERS_FILE"
log "  Sudoers configured for $ACTUAL_USER"

# -----------------------------------------------------------------------------
# 11. Move primitives .h5 files to skills directory
# -----------------------------------------------------------------------------
PRIMITIVES_DIR="$REPO_DIR/primitives"
if [ -d "$PRIMITIVES_DIR" ]; then
    log "Migrating primitives .h5 files to skills directory..."
    # Move each .h5 file to corresponding skills/ path (e.g. primitives/x/a.h5 -> skills/x/a.h5)
    find "$PRIMITIVES_DIR" -name "*.h5" -type f | while read -r h5_file; do
        # Remove the primitives directory path to get just the relative path (e.g. /path/primitives/x/a.h5 -> x/a.h5)
        rel_path="${h5_file#$PRIMITIVES_DIR/}"
        mkdir -p "$REPO_DIR/skills/$(dirname "$rel_path")"
        log "  Moving $rel_path"
        mv "$h5_file" "$REPO_DIR/skills/$rel_path"
    done
    
    # Remove any __pycache__ directories left behind
    find "$PRIMITIVES_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    
    # Remove primitives directory if no files remain
    if [ -z "$(find "$PRIMITIVES_DIR" -type f)" ]; then
        rm -rf "$PRIMITIVES_DIR"
        log "  Removed empty primitives directory"
    fi
fi

# Clean up directives directory if it exists
DIRECTIVES_DIR="$REPO_DIR/directives"
if [ -d "$DIRECTIVES_DIR" ]; then
    # Remove any __pycache__ directories left behind
    find "$DIRECTIVES_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    
    # Remove directives directory if no files remain
    if [ -z "$(find "$DIRECTIVES_DIR" -type f)" ]; then
        rm -rf "$DIRECTIVES_DIR"
        log "  Removed empty directives directory"
    fi
fi

# -----------------------------------------------------------------------------
# 11b. Download skill assets from metadata.json
# -----------------------------------------------------------------------------
for meta_file in "$REPO_DIR"/skills/*/metadata.json; do
    [ -f "$meta_file" ] || continue
    jq -r '.downloads // {} | to_entries[] | "\(.key)\t\(.value)"' "$meta_file" 2>/dev/null | \
    while IFS=$'\t' read -r fname url; do
        dest="$(dirname "$meta_file")/$fname"
        [ -f "$dest" ] && continue
        log "  Downloading $dest"
        sudo -u "$ACTUAL_USER" curl -fsSL -o "$dest.tmp" "$url"
        mv "$dest.tmp" "$dest"
    done
done

# -----------------------------------------------------------------------------
# 12. Enable and restart services
# -----------------------------------------------------------------------------
log "Enabling and starting services..."

# List of services to enable/start
SERVICES=("jetson-perf.service" "zenoh-router.service" "ros-app.service")

# Add ble-provisioner if the service file exists
if [ -f "/etc/systemd/system/ble-provisioner.service" ]; then
    SERVICES+=("ble-provisioner.service")
fi

# Add arducam-audio if the service file exists
if [ -f "/etc/systemd/system/arducam-audio.service" ]; then
    SERVICES+=("arducam-audio.service")
fi

# Add speaker-keepalive if the service file exists
if [ -f "/etc/systemd/system/speaker-keepalive.service" ]; then
    SERVICES+=("speaker-keepalive.service")
fi

# Add shutdown-sound if the service file exists (enable only, runs at shutdown)
if [ -f "/etc/systemd/system/shutdown-sound.service" ]; then
    SERVICES+=("shutdown-sound.service")
fi

# Add bluetooth if available
if systemctl list-unit-files bluetooth.service &>/dev/null; then
    SERVICES+=("bluetooth.service")
fi

# Always enable services (so they start on boot after reboot)
# But only start them if reboot is not required
for service in "${SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/$service" ] || systemctl list-unit-files "$service" &>/dev/null; then
        log "  Enabling $service"
        systemctl enable "$service" 2>/dev/null || true
        
        if [ "$HARDWARE_REBOOT_REQUIRED" = true ]; then
            log "  Skipping start of $service (reboot required - will start after reboot)"
        else
            log "  Starting $service"
            systemctl start "$service" 2>/dev/null || true
        fi
    fi
done

# -----------------------------------------------------------------------------
# 13. Launch ROS nodes in Tmux (optional, depends on ros-app.service)
# -----------------------------------------------------------------------------
# Note: If ros-app.service is configured correctly, it will launch the tmux session
# If not using systemd, you can manually launch:
# sudo -u "$ACTUAL_USER" INNATE_OS_ROOT="$REPO_DIR" bash "$REPO_DIR/scripts/launch_ros_in_tmux.sh" &

log_success "========================================"
log_success "Post-update script completed successfully"
log_success "========================================"

if [ "$HARDWARE_REBOOT_REQUIRED" != true ]; then
    log ""
    log "To start ROS manually:"
    log "  tmux attach -t ros_nodes  (if already running)"
    log "  OR"
    log "  launch_ros_in_tmux.sh     (to start fresh)"
    log ""
fi

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


