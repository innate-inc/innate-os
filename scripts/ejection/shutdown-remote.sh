#!/bin/bash
# Shutdown a robot via SSH
# Usage: ./shutdown-remote.sh [robot-user@robot-ip]
#
# Example: ./shutdown-remote.sh
#          ./shutdown-remote.sh jetson1@192.168.55.1
#          ./shutdown-remote.sh jetson1@192.168.1.100

set -e

# Configuration
ROBOT_PASSWORD="${ROBOT_PASSWORD:-goodbot}"

# Parse arguments
ROBOT_HOST="${1:-jetson1@mars.local}"

echo "═══════════════════════════════════════════════════════════════"
echo "  Shutting down robot"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Target: $ROBOT_HOST"
echo ""

ssh "$ROBOT_HOST" "echo '$ROBOT_PASSWORD' | sudo -S shutdown now" || true

echo "   ✓ Shutdown command sent"
echo ""



