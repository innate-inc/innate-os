#!/bin/bash
# Provision a robot: Install deploy key and run full setup
# Usage: ./provision-robot.sh <robot-number> [robot-user@robot-ip]
#
# Example: ./provision-robot.sh 1
#          ./provision-robot.sh 1 jetson1@192.168.55.1
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

if [ $# -lt 1 ]; then
    echo "Usage: $0 <robot-number> [robot-user@robot-ip]"
    echo ""
    echo "Example: $0 1"
    echo "         $0 1 jetson1@192.168.55.1"
    echo ""
    echo "This script will:"
    echo "  1. Install deploy key on the robot"
    echo "  2. Copy setup_robot_with.sh to the robot"
    echo "  3. Run setup_robot_with.sh on the robot"
    exit 1
fi

ROBOT_NUM="$1"
ROBOT_HOST="${2:-jetson1@192.168.55.1}"

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
ssh -tt "$ROBOT_HOST" bash << REMOTE_EOF
# Don't exit on error initially - we want to continue even if setup_robot_with.sh has issues
set +e
export ROBOT_PASSWORD="$ROBOT_PASSWORD"
export INNATE_OS_PATH="$INNATE_OS_PATH"
chmod +x /tmp/setup_robot_with.sh
cd /tmp
./setup_robot_with.sh $ROBOT_NUM
SETUP_EXIT_CODE=$?

# Clean up setup script
rm -f /tmp/setup_robot_with.sh
echo "✓ Cleaned up setup script"

# Continue even if setup script had errors
set +e
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
    python3 ./diagnostics.py 2>&1 || {
        echo "⚠️  Warning: diagnostics.py returned non-zero exit code"
    }
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
sudo shutdown now
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



