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

# Install PyTorch nightly with CUDA 12.8 for Blackwell (RTX 50xx, sm_120) support
# Stable torch builds top out at sm_90 and will fall back to CPU on RTX 5090
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "")
if echo "${GPU_NAME}" | grep -qi "RTX 50\|B200\|B100\|blackwell"; then
    echo "⚡ Blackwell GPU detected (${GPU_NAME}) - installing PyTorch nightly cu128..."
    uv pip install --python .venv/bin/python --pre --reinstall torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/nightly/cu128
    # Pin numpy to <2.3 (numba compatibility)
    uv pip install --python .venv/bin/python "numpy<2.3"
    echo "✅ PyTorch nightly cu128 installed"
fi

echo ""
echo "Setup complete! Activate the environment with:"
echo "  source .venv/bin/activate"
echo ""
echo "NOTE: You also need to download the ReplicaCAD datasets."
echo "See data/README.md for download instructions."
