#!/bin/bash
# Provision a robot: Install deploy key and run full setup
# Usage: ./provision-robot.sh <robot-number> [robot-user@robot-ip] [--skip-token]
#
# Example: ./provision-robot.sh 1
#          ./provision-robot.sh 1 jetson1@192.168.55.1
#          ./provision-robot.sh 1 jetson1@192.168.55.1 --skip-token
#
# This script:
#   1. Installs the deploy key on the robot (from install-key-on-robot.sh)
#   2. Copies setup_robot_with.sh to the robot
#   3. Runs setup_robot_with.sh on the robot

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_KEY_SCRIPT="$SCRIPT_DIR/install-key-on-robot.sh"
SETUP_SCRIPT="$SCRIPT_DIR/setup_robot_with.sh"

# Configuration
INNATE_OS_PATH="${INNATE_OS_PATH:-/home/jetson1/innate-os}"

# Parse arguments
SKIP_TOKEN=false
ROBOT_NUM=""
ROBOT_HOST=""

for arg in "$@"; do
    case $arg in
        --skip-token)
            SKIP_TOKEN=true
            shift
            ;;
        *)
            if [ -z "$ROBOT_NUM" ]; then
                ROBOT_NUM="$arg"
            elif [ -z "$ROBOT_HOST" ]; then
                ROBOT_HOST="$arg"
            fi
            ;;
    esac
done

if [ -z "$ROBOT_NUM" ]; then
    echo "Usage: $0 <robot-number> [robot-user@robot-ip] [--skip-token]"
    echo ""
    echo "Example: $0 1"
    echo "         $0 1 jetson1@192.168.55.1"
    echo "         $0 1 jetson1@192.168.55.1 --skip-token"
    echo ""
    echo "Options:"
    echo "  --skip-token    Skip token generation (use existing .env if present)"
    echo ""
    echo "This script will:"
    echo "  1. Install deploy key on the robot"
    echo "  2. Copy setup_robot_with.sh to the robot"
    echo "  3. Run setup_robot_with.sh on the robot"
    exit 1
fi

ROBOT_HOST="${ROBOT_HOST:-jetson1@192.168.55.1}"

# Verify scripts exist
if [ ! -f "$INSTALL_KEY_SCRIPT" ]; then
    echo "Error: install-key-on-robot.sh not found at $INSTALL_KEY_SCRIPT"
    exit 1
fi

if [ ! -f "$SETUP_SCRIPT" ]; then
    echo "Error: setup_robot_with.sh not found at $SETUP_SCRIPT"
    exit 1
fi

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║              Robot Provisioning Script                        ║"
echo "╠═══════════════════════════════════════════════════════════════╣"
echo "║  Robot Number: $ROBOT_NUM"
echo "║  Target Host:  $ROBOT_HOST"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

# =============================================================================
# Step 1: Install deploy key
# =============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  STEP 1: Installing deploy key"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

"$INSTALL_KEY_SCRIPT" "$ROBOT_NUM" "$ROBOT_HOST"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  STEP 1.5: Adding user to dialout group for serial port access"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Add user to dialout and i2c groups in a separate SSH session
# This ensures the group changes are applied before the main session
ROBOT_PASSWORD="${ROBOT_PASSWORD:-goodbot}"
ssh -t "$ROBOT_HOST" bash << GROUPS_EOF
set +e  # Don't exit on error, we want to handle it gracefully
export ROBOT_PASSWORD="$ROBOT_PASSWORD"

# Detect the actual user (not root)
CURRENT_USER="\$(whoami 2>/dev/null || id -un 2>/dev/null || echo 'jetson1')"
if [ "\$CURRENT_USER" = "root" ]; then
    CURRENT_USER="jetson1"
fi

# Function to add user to a group
add_to_group() {
    local group=\$1
    local group_display=\$2
    
    # Check if user is already in the group
    if groups "\$CURRENT_USER" 2>/dev/null | grep -q "\b\$group\b"; then
        echo "✓ User \$CURRENT_USER is already in \$group_display group"
        return 0
    fi
    
    # User not in group, add them
    echo "Adding user \$CURRENT_USER to \$group_display group..."
    if sudo -n true 2>/dev/null; then
        sudo usermod -aG "\$group" "\$CURRENT_USER" 2>&1
        ADD_EXIT=\$?
    else
        echo "\$ROBOT_PASSWORD" | sudo -S usermod -aG "\$group" "\$CURRENT_USER" >/dev/null 2>&1
        ADD_EXIT=\$?
    fi
    
    if [ \$ADD_EXIT -eq 0 ]; then
        echo "✓ User \$CURRENT_USER added to \$group_display group"
        return 0
    else
        echo "⚠️  Warning: Failed to add user to \$group_display group (exit code: \$ADD_EXIT)"
        return 1
    fi
}

# Add to dialout group (for serial port access)
add_to_group "dialout" "dialout"

# Add to i2c group (for I2C bus access)
add_to_group "i2c" "i2c"

# Explicitly close the session
exit 0
GROUPS_EOF

DIALOUT_EXIT=$?
if [ $DIALOUT_EXIT -ne 0 ]; then
    echo "⚠️  Warning: Failed to add user to dialout group, but continuing..."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  STEP 2: Copying setup script to robot"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Copy setup script to robot
scp "$SETUP_SCRIPT" "$ROBOT_HOST:/tmp/setup_robot_with.sh"
echo "✓ Setup script copied to /tmp/setup_robot_with.sh"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  STEP 3: Running setup script on robot"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Run setup script on robot in a SEPARATE SSH session to avoid heredoc conflicts
# This prevents setup_robot_with.sh's internal heredocs from consuming the parent heredoc
ROBOT_PASSWORD="${ROBOT_PASSWORD:-goodbot}"
INNATE_OS_PATH="${INNATE_OS_PATH:-/home/jetson1/innate-os}"
SKIP_TOKEN_FLAG="${SKIP_TOKEN:-false}"

# Preserve .env file before setup (in case setup overwrites it)
echo "Preserving .env file..."
ENV_BACKUP=$(ssh "$ROBOT_HOST" "cat $INNATE_OS_PATH/.env 2>/dev/null" || echo "")

# Run setup script in separate SSH session to completely isolate it from heredoc
echo "Running setup_robot_with.sh..."
if [ "$SKIP_TOKEN_FLAG" = "true" ]; then
    ssh -tt "$ROBOT_HOST" "export ROBOT_PASSWORD='$ROBOT_PASSWORD'; export INNATE_OS_PATH='$INNATE_OS_PATH'; cd /tmp && chmod +x /tmp/setup_robot_with.sh && bash /tmp/setup_robot_with.sh $ROBOT_NUM --skip-token"
    SETUP_EXIT_CODE=$?
else
    ssh -tt "$ROBOT_HOST" "export ROBOT_PASSWORD='$ROBOT_PASSWORD'; export INNATE_OS_PATH='$INNATE_OS_PATH'; cd /tmp && chmod +x /tmp/setup_robot_with.sh && bash /tmp/setup_robot_with.sh $ROBOT_NUM"
    SETUP_EXIT_CODE=$?
fi

# Now continue with the rest in the heredoc
ssh -tt "$ROBOT_HOST" bash << REMOTE_EOF
# Don't exit on error - we want to continue even if setup_robot_with.sh had issues
set +e
export ROBOT_PASSWORD="$ROBOT_PASSWORD"
export INNATE_OS_PATH="$INNATE_OS_PATH"
SETUP_EXIT_CODE=$SETUP_EXIT_CODE

# Restore .env file if it was backed up and doesn't exist or was overwritten
ENV_BACKUP='$ENV_BACKUP'
if [ -n "\$ENV_BACKUP" ]; then
    if [ ! -f "\$INNATE_OS_PATH/.env" ] || ! grep -q "INNATE_SERVICE_KEY=" "\$INNATE_OS_PATH/.env" 2>/dev/null; then
        echo "\$ENV_BACKUP" > "\$INNATE_OS_PATH/.env"
        echo "✓ Restored .env file from backup"
    fi
fi

# Clean up setup script
rm -f /tmp/setup_robot_with.sh
echo "✓ Cleaned up setup script"
echo "  Setup script exit code: $SETUP_EXIT_CODE"

# CRITICAL: Force continuation - add explicit markers to verify heredoc continues
echo ""
echo ">>> SETUP_SCRIPT_COMPLETED - CONTINUING TO POST_UPDATE <<<"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Continuing with post-update and diagnostics..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Running post_update script..."
if [ -d "\$INNATE_OS_PATH/scripts/update" ]; then
    cd "\$INNATE_OS_PATH/scripts/update"
    if [ -f "./post_update.sh" ]; then
        chmod +x ./post_update.sh
        echo "\$ROBOT_PASSWORD" | sudo -S ./post_update.sh || {
            echo "⚠️  Warning: post_update.sh failed, but continuing..."
        }
        echo "✓ Post-update script completed"
    else
        echo "⚠️  Warning: post_update.sh not found at \$INNATE_OS_PATH/scripts/update/post_update.sh"
        echo "   Skipping post-update step"
    fi
else
    echo "⚠️  Warning: \$INNATE_OS_PATH/scripts/update directory not found"
    echo "   Skipping post-update step"
fi

# Run diagnostics
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Running system diagnostics..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cd "\$INNATE_OS_PATH/scripts"
if [ -f "./diagnostics.py" ]; then
    # Try to run diagnostics with dialout group if user is in it
    if groups | grep -q "\bdialout\b"; then
        # User is in dialout group, run diagnostics with that group active
        sg dialout -c "cd \$INNATE_OS_PATH/scripts && python3 ./diagnostics.py" 2>&1 || {
            echo "⚠️  Warning: diagnostics.py returned non-zero exit code"
        }
    else
        # User not in dialout group yet, run diagnostics anyway (will show permission warning)
        python3 ./diagnostics.py 2>&1 || {
            echo "⚠️  Warning: diagnostics.py returned non-zero exit code"
        }
    fi
    echo "✓ Diagnostics completed"
else
    echo "⚠️  Warning: diagnostics.py not found at \$INNATE_OS_PATH/scripts/diagnostics.py"
fi

# Copy arm_wave data to primitives/wave folder
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Copying arm_wave data to primitives/wave..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ -d "\$INNATE_OS_PATH/primitives/wave" ]; then
    echo "✓ primitives/wave directory exists"
else
    echo "Creating primitives/wave directory..."
    mkdir -p "\$INNATE_OS_PATH/primitives/wave"
    echo "✓ Created primitives/wave directory"
fi
REMOTE_EOF

# Wait a moment for SSH session to close
sleep 1

# Copy arm_wave files to robot (run from local machine)
# arm_wave is expected to be in the parent directory of innate-os (same level as innate-os)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Copying arm_wave data to primitives/wave..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
ARM_WAVE_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")/arm_wave"
if [ -d "$ARM_WAVE_DIR" ]; then
    echo "Found arm_wave directory at: $ARM_WAVE_DIR"
    # Ensure target directory exists (redundant but safe)
    ssh "$ROBOT_HOST" "mkdir -p $INNATE_OS_PATH/primitives/wave"
    # Always copy files, even if directory already exists (overwrites existing files)
    echo "Copying arm_wave files to robot (will overwrite existing files)..."
    scp -r "$ARM_WAVE_DIR"/* "$ROBOT_HOST:$INNATE_OS_PATH/primitives/wave/" || {
        echo "⚠️  Warning: Failed to copy arm_wave files, but continuing..."
    }
    echo "✓ arm_wave files copied to primitives/wave"
else
    echo "⚠️  Warning: arm_wave directory not found at $ARM_WAVE_DIR, skipping..."
    echo "  Expected location: $(dirname "$(dirname "$SCRIPT_DIR")")/arm_wave"
fi

# Continue with shutdown in a new SSH session
ssh -tt "$ROBOT_HOST" bash << SHUTDOWN_EOF
set +e
export ROBOT_PASSWORD="$ROBOT_PASSWORD"

# Shutdown after 3 seconds
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Shutting down robot in 3 seconds..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
sleep 3
# Use password for sudo shutdown
if sudo -n true 2>/dev/null; then
    sudo shutdown now
else
    echo "\$ROBOT_PASSWORD" | sudo -S shutdown now
fi
set -e
SHUTDOWN_EOF

echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  ✓ Robot $ROBOT_NUM provisioning complete!                    "
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""
echo "The robot has been configured with:"
echo "  - Deploy key installed and SSH configured"
echo "  - Git remote set to release repository"
echo "  - Full robot setup completed (token, calibration, ROS, etc.)"
echo ""
echo "To verify, SSH into the robot:"
echo "  ssh $ROBOT_HOST"
echo ""



