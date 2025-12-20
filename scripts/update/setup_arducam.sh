#!/bin/bash
# Arducam Microphone Setup Script
# Equivalent to setup_arducam.py but as a standalone shell script
#
# Usage: ./setup_arducam.sh
#   or:  sudo ./setup_arducam.sh  (if running from post_update.sh context)

set -e

echo "=================================================="
echo "🎙️  Arducam Microphone Setup Script"
echo "=================================================="

# Determine the actual user (handle both sudo and non-sudo cases)
if [ -n "$SUDO_USER" ]; then
    ACTUAL_USER="$SUDO_USER"
    ACTUAL_HOME=$(eval echo ~$SUDO_USER)
else
    ACTUAL_USER="$USER"
    ACTUAL_HOME="$HOME"
fi

echo "Running as user: $ACTUAL_USER"

# -----------------------------------------------------------------------------
# Step 1: Find Arducam ALSA device
# -----------------------------------------------------------------------------
echo ""
echo "🔍 Searching for Arducam device..."
arecord -l 2>/dev/null || true

# Look for Arducam specifically, then fall back to USB audio, then any capture device
ARDUCAM_CARD=$(arecord -l 2>/dev/null | grep -i 'arducam' | head -1 | sed -n 's/card \([0-9]*\):.*/\1/p')

if [ -z "$ARDUCAM_CARD" ]; then
    # Fallback: try USB audio
    ARDUCAM_CARD=$(arecord -l 2>/dev/null | grep -i -E 'usb audio|camera|uac' | head -1 | sed -n 's/card \([0-9]*\):.*/\1/p')
fi

if [ -z "$ARDUCAM_CARD" ]; then
    # Last resort: first capture device
    ARDUCAM_CARD=$(arecord -l 2>/dev/null | head -1 | sed -n 's/card \([0-9]*\):.*/\1/p')
    if [ -n "$ARDUCAM_CARD" ]; then
        echo "⚠️  Using first available card $ARDUCAM_CARD"
    fi
fi

if [ -z "$ARDUCAM_CARD" ]; then
    echo "❌ No recording devices found. Is the Arducam connected?"
    exit 1
fi

echo "✅ Found mic on ALSA card $ARDUCAM_CARD"

# -----------------------------------------------------------------------------
# Step 2: Configure ALSA mixer (equivalent to alsamixer)
# -----------------------------------------------------------------------------
echo ""
echo "🔧 Configuring ALSA mixer for card $ARDUCAM_CARD..."

# List available controls
echo "📋 Available mixer controls:"
amixer -c "$ARDUCAM_CARD" scontrols 2>/dev/null || true

# Try various control names that microphones might use
for control in Mic Capture Digital Input PCM; do
    # Try to enable capture
    if amixer -c "$ARDUCAM_CARD" sset "$control" cap 2>/dev/null | grep -q -v "Invalid\|Unable"; then
        echo "  ✓ Enabled capture for '$control'"
    fi
    
    # Try to set volume to 100%
    if amixer -c "$ARDUCAM_CARD" sset "$control" 100% 2>/dev/null | grep -q -v "Invalid\|Unable"; then
        echo "  ✓ Set volume for '$control' to 100%"
    fi
    
    # Try to unmute
    if amixer -c "$ARDUCAM_CARD" sset "$control" unmute 2>/dev/null | grep -q -v "Invalid\|Unable"; then
        echo "  ✓ Unmuted '$control'"
    fi
done

# Also try the combined approach
amixer -c "$ARDUCAM_CARD" sset Capture 100% cap unmute 2>/dev/null && \
    echo "  ✓ Set Capture to 100% and enabled" || true

# -----------------------------------------------------------------------------
# Step 3: Save ALSA settings
# -----------------------------------------------------------------------------
echo ""
echo "💾 Saving ALSA settings..."

if alsactl store 2>/dev/null; then
    echo "  ✓ ALSA settings saved to /var/lib/alsa/asound.state"
elif sudo alsactl store 2>/dev/null; then
    echo "  ✓ ALSA settings saved (via sudo)"
else
    echo "  ⚠️  Failed to save ALSA settings - run: sudo alsactl store"
fi

# -----------------------------------------------------------------------------
# Step 4: Find PulseAudio source
# -----------------------------------------------------------------------------
echo ""
echo "🔍 Searching for PulseAudio source..."

# Wait a moment for PulseAudio to detect the device
sleep 1

# List all sources
echo "Available sources:"
if [ -n "$SUDO_USER" ]; then
    sudo -u "$ACTUAL_USER" XDG_RUNTIME_DIR="/run/user/$(id -u $ACTUAL_USER)" \
        pactl list short sources 2>/dev/null || true
else
    pactl list short sources 2>/dev/null || true
fi

# Find the Arducam source (skip monitor sources)
if [ -n "$SUDO_USER" ]; then
    ARDUCAM_SOURCE=$(sudo -u "$ACTUAL_USER" XDG_RUNTIME_DIR="/run/user/$(id -u $ACTUAL_USER)" \
        pactl list short sources 2>/dev/null | grep -i arducam | grep -v monitor | head -1 | cut -f2)
else
    ARDUCAM_SOURCE=$(pactl list short sources 2>/dev/null | grep -i arducam | grep -v monitor | head -1 | cut -f2)
fi

# Fallback to USB audio
if [ -z "$ARDUCAM_SOURCE" ]; then
    if [ -n "$SUDO_USER" ]; then
        ARDUCAM_SOURCE=$(sudo -u "$ACTUAL_USER" XDG_RUNTIME_DIR="/run/user/$(id -u $ACTUAL_USER)" \
            pactl list short sources 2>/dev/null | grep -i -E 'usb|camera|uac' | grep -v monitor | head -1 | cut -f2)
    else
        ARDUCAM_SOURCE=$(pactl list short sources 2>/dev/null | grep -i -E 'usb|camera|uac' | grep -v monitor | head -1 | cut -f2)
    fi
    if [ -n "$ARDUCAM_SOURCE" ]; then
        echo "⚠️  Using USB audio source: $ARDUCAM_SOURCE"
    fi
fi

# Fallback to first non-monitor source
if [ -z "$ARDUCAM_SOURCE" ]; then
    if [ -n "$SUDO_USER" ]; then
        ARDUCAM_SOURCE=$(sudo -u "$ACTUAL_USER" XDG_RUNTIME_DIR="/run/user/$(id -u $ACTUAL_USER)" \
            pactl list short sources 2>/dev/null | grep -v monitor | head -1 | cut -f2)
    else
        ARDUCAM_SOURCE=$(pactl list short sources 2>/dev/null | grep -v monitor | head -1 | cut -f2)
    fi
    if [ -n "$ARDUCAM_SOURCE" ]; then
        echo "⚠️  Using first available source: $ARDUCAM_SOURCE"
    fi
fi

if [ -z "$ARDUCAM_SOURCE" ]; then
    echo "❌ No suitable PulseAudio source found"
    echo "   The mic should still work via ALSA directly."
    exit 0
fi

echo "✅ Found source: $ARDUCAM_SOURCE"

# -----------------------------------------------------------------------------
# Step 5: Configure PulseAudio
# -----------------------------------------------------------------------------
echo ""
echo "🔧 Configuring PulseAudio source: $ARDUCAM_SOURCE"

run_pactl() {
    if [ -n "$SUDO_USER" ]; then
        sudo -u "$ACTUAL_USER" XDG_RUNTIME_DIR="/run/user/$(id -u $ACTUAL_USER)" pactl "$@"
    else
        pactl "$@"
    fi
}

# Set as default source
if run_pactl set-default-source "$ARDUCAM_SOURCE" 2>/dev/null; then
    echo "  ✓ Set as default source"
else
    echo "  ⚠️  Failed to set as default source"
fi

# Unmute the source
if run_pactl set-source-mute @DEFAULT_SOURCE@ 0 2>/dev/null; then
    echo "  ✓ Unmuted source"
else
    echo "  ⚠️  Failed to unmute source"
fi

# Set volume to 100%
if run_pactl set-source-volume @DEFAULT_SOURCE@ 100% 2>/dev/null; then
    echo "  ✓ Set volume to 100%"
else
    echo "  ⚠️  Failed to set volume"
fi

# -----------------------------------------------------------------------------
# Step 6: Verify setup
# -----------------------------------------------------------------------------
echo ""
echo "🔍 Verifying setup..."

DEFAULT_SOURCE=$(run_pactl get-default-source 2>/dev/null)
echo "  Default source: $DEFAULT_SOURCE"

# Show mute/volume status
run_pactl list sources 2>/dev/null | grep -A 20 "Name: $DEFAULT_SOURCE" | grep -E "Mute:|Volume:" | head -2 | while read line; do
    echo "  $line"
done

echo ""
echo "=================================================="
echo "✅ Setup complete!"
echo ""
echo "To test the microphone, run:"
echo "  python3 test_mic.py"
echo "  # or"
echo "  arecord -d 3 -f cd test.wav && aplay test.wav"
echo "=================================================="

