#!/bin/bash
set -e

# Detect OS and select appropriate requirements file
if [[ "$OSTYPE" == "darwin"* ]]; then
    REQUIREMENTS_FILE="requirements.macos.txt"
    echo "Detected macOS, using $REQUIREMENTS_FILE"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    REQUIREMENTS_FILE="requirements.ubuntu.txt"
    echo "Detected Linux, using $REQUIREMENTS_FILE"
else
    echo "Unsupported OS: $OSTYPE"
    exit 1
fi

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "uv is not installed. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the shell profile to make uv available
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Creating virtual environment with Python 3.11..."
uv venv --python 3.11

echo "Installing dependencies from $REQUIREMENTS_FILE..."
uv pip install -r "$REQUIREMENTS_FILE" --python .venv/bin/python

echo ""
echo "Setup complete! Activate the environment with:"
echo "  source .venv/bin/activate"
echo ""
echo "NOTE: You also need to download the ReplicaCAD datasets."
echo "See data/README.md for download instructions."
