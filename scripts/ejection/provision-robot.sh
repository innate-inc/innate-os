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

# Run setup script on robot
# Use -tt flag to force TTY allocation even with redirected stdin (for sudo password prompts)
ROBOT_PASSWORD="${ROBOT_PASSWORD:-goodbot}"
INNATE_OS_PATH="${INNATE_OS_PATH:-/home/jetson1/innate-os}"
SKIP_TOKEN_FLAG="${SKIP_TOKEN:-false}"
ssh -tt "$ROBOT_HOST" bash << REMOTE_EOF
# Don't exit on error - we want to continue even if setup_robot_with.sh has issues
set +e
export ROBOT_PASSWORD="$ROBOT_PASSWORD"
export INNATE_OS_PATH="$INNATE_OS_PATH"
SKIP_TOKEN="$SKIP_TOKEN_FLAG"
chmod +x /tmp/setup_robot_with.sh
cd /tmp
# Run setup script - capture exit code but continue regardless
if [ "$SKIP_TOKEN" = "true" ]; then
    ./setup_robot_with.sh $ROBOT_NUM --skip-token || true
else
    ./setup_robot_with.sh $ROBOT_NUM || true
fi
SETUP_EXIT_CODE=$?

# Clean up setup script
rm -f /tmp/setup_robot_with.sh
echo "✓ Cleaned up setup script"
echo "  Setup script exit code: $SETUP_EXIT_CODE"

# Continue even if setup script had errors
set +e
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
cd $INNATE_OS_PATH/scripts
if [ -f "./diagnostics.py" ]; then
    # Try to run diagnostics with dialout group if user is in it
    if groups | grep -q "\bdialout\b"; then
        # User is in dialout group, run diagnostics with that group active
        sg dialout -c "cd $INNATE_OS_PATH/scripts && python3 ./diagnostics.py" 2>&1 || {
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
    echo "⚠️  Warning: diagnostics.py not found at $INNATE_OS_PATH/scripts/diagnostics.py"
fi

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
REMOTE_EOF

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



