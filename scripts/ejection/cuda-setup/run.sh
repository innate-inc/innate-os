#!/bin/bash
# Run the CUDA Setup web interface
# Usage: ./run.sh [robot-user@robot-ip]
#
# Example: ./run.sh
#          ./run.sh jetson1@192.168.55.1

cd "$(dirname "$0")"

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

# Set robot host from argument or use default
export ROBOT_HOST="${1:-jetson1@192.168.55.1}"
export ROBOT_PASSWORD="${ROBOT_PASSWORD:-goodbot}"

echo ""
echo "Starting CUDA Setup..."
echo "Robot: $ROBOT_HOST"
echo ""

python3 app.py

