#!/bin/bash
# Run the Robot Manager web interface
# Usage: ./run.sh [--ip <ip-address>] [robot-user@robot-ip]
#
# Example: ./run.sh
#          ./run.sh --ip 192.168.55.1
#          ./run.sh jetson1@192.168.55.1

cd "$(dirname "$0")"

# Configuration
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

# Check for sshpass
if ! command -v sshpass &> /dev/null; then
    echo "⚠️  sshpass not found. Installing..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install hudochenkov/sshpass/sshpass
    else
        sudo apt-get install -y sshpass
    fi
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies if needed
if ! python3 -c "import flask" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

# Set robot host
export ROBOT_HOST
export ROBOT_PASSWORD="${ROBOT_PASSWORD:-goodbot}"

echo ""
echo "Starting Robot Manager..."
echo "Robot: $ROBOT_HOST"
echo ""

python3 app.py

