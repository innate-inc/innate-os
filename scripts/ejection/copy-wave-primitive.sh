#!/bin/bash
# Copy wave primitive h5 file to a robot via SSH
# Usage: ./copy-wave-primitive.sh [--ip <ip-address>] [robot-user@robot-ip]
#
# Example: ./copy-wave-primitive.sh
#          ./copy-wave-primitive.sh --ip 192.168.55.1
#          ./copy-wave-primitive.sh jetson1@192.168.55.1
#          ./copy-wave-primitive.sh jetson1@192.168.1.100

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INNATE_OS_LOCAL="$(cd "$SCRIPT_DIR/../.." && pwd)"
INNATE_OS_REMOTE="${INNATE_OS_REMOTE:-/home/jetson1/innate-os}"
WAVE_DIR="primitives/wave"
DEFAULT_USER="jetson1"

# Source and destination paths
LOCAL_WAVE_DIR="$INNATE_OS_LOCAL/$WAVE_DIR"
REMOTE_WAVE_DIR="$INNATE_OS_REMOTE/$WAVE_DIR"

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

# Verify local wave directory exists
if [ ! -d "$LOCAL_WAVE_DIR" ]; then
    echo "Error: Wave primitive directory not found at $LOCAL_WAVE_DIR"
    exit 1
fi

# Find h5 files in the wave directory
H5_FILES=$(find "$LOCAL_WAVE_DIR" -name "*.h5" 2>/dev/null)
if [ -z "$H5_FILES" ]; then
    echo "Error: No .h5 files found in $LOCAL_WAVE_DIR"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Copying wave primitive to robot"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Source:      $LOCAL_WAVE_DIR"
echo "  Destination: $ROBOT_HOST:$REMOTE_WAVE_DIR"
echo ""
echo "  Files to copy:"
for file in $H5_FILES; do
    echo "    - $(basename "$file") ($(du -h "$file" | cut -f1))"
done
echo ""

# Copy h5 files
echo "Copying h5 files..."
for file in $H5_FILES; do
    filename=$(basename "$file")
    echo "   Copying $filename..."
    scp "$file" "$ROBOT_HOST:$REMOTE_WAVE_DIR/$filename"
    echo "   ✓ $filename copied"
done

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✓ Wave primitive copied successfully"
echo "═══════════════════════════════════════════════════════════════"
echo ""

