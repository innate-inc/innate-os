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

# Ensure ALSA restore service is enabled (for boot persistence)
echo "🔧 Ensuring ALSA restore service is enabled..."
if systemctl is-enabled alsa-restore.service >/dev/null 2>&1; then
    echo "  ✓ alsa-restore.service already enabled"
elif systemctl enable alsa-restore.service 2>/dev/null; then
    echo "  ✓ Enabled alsa-restore.service"
elif sudo systemctl enable alsa-restore.service 2>/dev/null; then
    echo "  ✓ Enabled alsa-restore.service (via sudo)"
else
    echo "  ⚠️  Could not enable alsa-restore.service (ALSA settings may not persist)"
fi

# -----------------------------------------------------------------------------
# Step 4: Verify setup
# -----------------------------------------------------------------------------
echo ""
echo "🔍 Verifying ALSA setup..."
amixer -c "$ARDUCAM_CARD" scontents 2>/dev/null | head -20 || true

echo ""
echo "=================================================="
echo "✅ Setup complete!"
echo ""
echo "Settings will persist after reboot:"
echo "  - ALSA: saved to /var/lib/alsa/asound.state"
echo ""
echo "To test the microphone, run:"
echo "  python3 test_mic.py"
echo "  # or"
echo "  arecord -d 3 -f cd test.wav && aplay test.wav"
echo "=================================================="

