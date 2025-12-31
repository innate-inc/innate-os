#!/bin/bash
# Remove all map files from a robot via SSH
# Usage: ./clear-remote-maps.sh [--ip <ip-address>] [robot-user@robot-ip]
#
# Example: ./clear-remote-maps.sh
#          ./clear-remote-maps.sh --ip 192.168.55.1
#          ./clear-remote-maps.sh jetson1@192.168.55.1
#          ./clear-remote-maps.sh jetson1@192.168.1.100

set -e

# Configuration
INNATE_OS_REMOTE="${INNATE_OS_REMOTE:-/home/jetson1/innate-os}"
MAPS_DIR="$INNATE_OS_REMOTE/maps"
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
echo "  Clearing maps from robot"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Target: $ROBOT_HOST:$MAPS_DIR"
echo ""

# List and remove map files on remote
ssh "$ROBOT_HOST" bash << REMOTE_EOF
set -e
MAPS_DIR="$MAPS_DIR"

# Check if maps directory exists
if [ ! -d "\$MAPS_DIR" ]; then
    echo "   Maps directory does not exist: \$MAPS_DIR"
    echo "   Nothing to clear."
    exit 0
fi

# Find map files (*.pgm and *.yaml)
PGM_FILES=\$(find "\$MAPS_DIR" -name "*.pgm" 2>/dev/null || true)
YAML_FILES=\$(find "\$MAPS_DIR" -name "*.yaml" 2>/dev/null || true)

if [ -z "\$PGM_FILES" ] && [ -z "\$YAML_FILES" ]; then
    echo "   No map files found in \$MAPS_DIR"
    echo "   Nothing to clear."
    exit 0
fi

# List files to be removed
echo "   Files to be removed:"
for file in \$PGM_FILES \$YAML_FILES; do
    if [ -f "\$file" ]; then
        echo "     - \$(basename "\$file")"
    fi
done
echo ""

# Remove the files
echo "   Removing map files..."
rm -f "\$MAPS_DIR"/*.pgm 2>/dev/null || true
rm -f "\$MAPS_DIR"/*.yaml 2>/dev/null || true
echo "   ✓ Map files removed"

# Verify
echo ""
echo "   Remaining files in \$MAPS_DIR:"
REMAINING=\$(ls -A "\$MAPS_DIR" 2>/dev/null | grep -v "^\.gitignore\$" || true)
if [ -z "\$REMAINING" ]; then
    echo "     (none - directory is clean)"
else
    ls -la "\$MAPS_DIR" 2>/dev/null | tail -n +2 | while read line; do
        echo "     \$line"
    done
fi
REMOTE_EOF

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✓ Maps cleared successfully"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Play speaker test for 15 seconds
echo "Playing speaker test for 15 seconds..."
ssh "$ROBOT_HOST" "timeout 10 speaker-test -t sine -c 1" || true
echo "   ✓ Speaker test complete"
echo ""

