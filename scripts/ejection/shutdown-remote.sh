#!/bin/bash
# Shutdown a robot via SSH
# Usage: ./shutdown-remote.sh [--ip <ip-address>] [robot-user@robot-ip]
#
# Example: ./shutdown-remote.sh
#          ./shutdown-remote.sh --ip 192.168.55.1
#          ./shutdown-remote.sh jetson1@192.168.55.1
#          ./shutdown-remote.sh jetson1@192.168.1.100

set -e

# Configuration
ROBOT_PASSWORD="${ROBOT_PASSWORD:-goodbot}"
DEFAULT_USER="jetson1"

# Parse arguments
ROBOT_HOST=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --ip|-i)
            ROBOT_HOST="${DEFAULT_USER}@$2"
            shift 2
            ;;
        *)
            if [ -z "$ROBOT_HOST" ]; then
                # If it looks like just an IP address, prepend the default user
                if [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                    ROBOT_HOST="${DEFAULT_USER}@$1"
                else
                    ROBOT_HOST="$1"
                fi
            fi
            shift
            ;;
    esac
done

ROBOT_HOST="${ROBOT_HOST:-${DEFAULT_USER}@mars.local}"

echo "═══════════════════════════════════════════════════════════════"
echo "  Shutting down robot"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Target: $ROBOT_HOST"
echo ""

ssh "$ROBOT_HOST" "echo '$ROBOT_PASSWORD' | sudo -S shutdown now" || true

echo "   ✓ Shutdown command sent"
echo ""



