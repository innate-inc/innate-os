#!/bin/bash
# Hardware Configuration Script for Innate-OS
# This script configures hardware-related system settings
# Requires root privileges via sudo
#
# Usage: sudo ./configure_hardware.sh <REPO_DIR>
#
# This script handles:
#   - I2S audio amplifier (MAX98357A via Adafruit UDA1334A overlay)
#   - Bluetooth configuration
#   - Arducam microphone setup (ALSA + PulseAudio)
#   - WiFi power management (disable power saving for stable ROS/DDS)

set -e  # Exit on error

# Check for root privileges
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root. Please use sudo." >&2
    exit 1
fi

# Get REPO_DIR from argument
REPO_DIR="${1:-}"
if [ -z "$REPO_DIR" ]; then
    echo "Usage: $0 <REPO_DIR>" >&2
    exit 1
fi

# Logging function (standalone or inherited from caller)
if ! type log &>/dev/null; then
    log() {
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
    }
fi

REBOOT_REQUIRED=false

# -----------------------------------------------------------------------------
# 1. I2S Audio Amplifier (MAX98357A)
# -----------------------------------------------------------------------------
log "Checking I2S audio configuration..."

# Only run on Jetson (check if jetson-io exists)
if [ -f "/opt/nvidia/jetson-io/config-by-hardware.py" ]; then
    # Check if Adafruit UDA1334A overlay is already configured
    if grep -q "UDA1334" /boot/extlinux/extlinux.conf 2>/dev/null; then
        log "  I2S audio overlay already configured"
    else
        log "  Configuring I2S audio overlay (Adafruit UDA1334A for MAX98357A)..."
        if /opt/nvidia/jetson-io/config-by-hardware.py -n "Adafruit UDA1334A" 2>&1; then
            log "  I2S audio overlay configured successfully"
            REBOOT_REQUIRED=true
        else
            log "  WARNING: Failed to configure I2S audio overlay"
        fi
    fi
else
    log "  Skipping I2S config - not a Jetson device"
fi

# -----------------------------------------------------------------------------
# 2. Bluetooth Configuration
# -----------------------------------------------------------------------------
log "Checking Bluetooth configurations..."
if [ -f "$REPO_DIR/config/bluetooth/main.conf" ]; then
    if [ -d "/etc/bluetooth" ]; then
        log "  Updating /etc/bluetooth/main.conf"
        rm -f /etc/bluetooth/main.conf
        cp "$REPO_DIR/config/bluetooth/main.conf" /etc/bluetooth/main.conf
    else
        log "  Skipping bluetooth config - /etc/bluetooth not found (VM or no bluetooth)"
    fi
fi

if [ -f "$REPO_DIR/config/bluetooth/nv-bluetooth-service.conf" ]; then
    if [ -d "/lib/systemd/system" ]; then
        log "  Updating bluetooth service override"
        mkdir -p /lib/systemd/system/bluetooth.service.d/
        rm -f /lib/systemd/system/bluetooth.service.d/nv-bluetooth-service.conf
        cp "$REPO_DIR/config/bluetooth/nv-bluetooth-service.conf" /lib/systemd/system/bluetooth.service.d/nv-bluetooth-service.conf
        systemctl daemon-reload
    else
        log "  Skipping bluetooth service override - systemd not found"
    fi
fi

# -----------------------------------------------------------------------------
# 3. Arducam Microphone (ALSA + PulseAudio)
# -----------------------------------------------------------------------------
log "Configuring Arducam microphone..."

ARDUCAM_SCRIPT="$REPO_DIR/scripts/update/setup_arducam.sh"
if [ -f "$ARDUCAM_SCRIPT" ]; then
    chmod +x "$ARDUCAM_SCRIPT"
    if "$ARDUCAM_SCRIPT"; then
        log "  Arducam setup completed successfully"
    else
        log "  WARNING: Arducam setup script failed (microphone may not work)"
    fi
else
    log "  WARNING: setup_arducam.sh not found at $ARDUCAM_SCRIPT"
fi

# -----------------------------------------------------------------------------
# 4. WiFi Power Management
# -----------------------------------------------------------------------------
log "Configuring WiFi power management..."

WIFI_POWERSAVE_CONF="/etc/NetworkManager/conf.d/default-wifi-powersave-off.conf"
WIFI_POWERSAVE_ON_CONF="/etc/NetworkManager/conf.d/default-wifi-powersave-on.conf"

# Create NetworkManager config directory if it doesn't exist
mkdir -p /etc/NetworkManager/conf.d

# Create config to disable WiFi power saving
cat > "$WIFI_POWERSAVE_CONF" << 'EOF'
[connection]
wifi.powersave = 2
EOF
log "  Created $WIFI_POWERSAVE_CONF"

# Remove conflicting "on" config if it exists
if [ -f "$WIFI_POWERSAVE_ON_CONF" ]; then
    rm -f "$WIFI_POWERSAVE_ON_CONF"
    log "  Removed conflicting $WIFI_POWERSAVE_ON_CONF"
fi

# Note: Not restarting NetworkManager here to avoid SSH disconnection.
# The config will be applied on next reboot or manual NetworkManager restart.

# -----------------------------------------------------------------------------
# 5. Boot Timeout Configuration
# -----------------------------------------------------------------------------
log "Checking boot timeout configuration..."

EXTLINUX_CONF="/boot/extlinux/extlinux.conf"
if [ -f "$EXTLINUX_CONF" ]; then
    # Check if there's a TIMEOUT line that is not already "TIMEOUT 0"
    if grep -q "TIMEOUT" "$EXTLINUX_CONF" && ! grep -q "TIMEOUT 0" "$EXTLINUX_CONF"; then
        log "  Modifying boot timeout to 0..."
        # Create a backup
        cp "$EXTLINUX_CONF" "${EXTLINUX_CONF}.bak.$(date +%Y%m%d%H%M%S)"
        
        # Replace TIMEOUT <number> with TIMEOUT 0
        sed -i 's/TIMEOUT [0-9]\+/TIMEOUT 0/' "$EXTLINUX_CONF"
        
        log "  Boot timeout set to 0"
        REBOOT_REQUIRED=true
    elif grep -q "TIMEOUT 0" "$EXTLINUX_CONF"; then
        log "  Boot timeout already set to 0"
    else
        log "  No TIMEOUT line found in extlinux.conf"
    fi
else
    log "  Skipping boot timeout config - $EXTLINUX_CONF not found"
fi

# -----------------------------------------------------------------------------
# 6. Disable Unnecessary Services
# -----------------------------------------------------------------------------
log "Configuring system services..."

# Mask services that are not needed or slow down boot
SERVICES_TO_MASK=(
    "lvm2-monitor.service"
    "lvm2-monitor-early.service"
    "snapd.service"
)

for service in "${SERVICES_TO_MASK[@]}"; do
    if systemctl list-unit-files "$service" &>/dev/null; then
        if ! systemctl is-masked "$service" &>/dev/null; then
            log "  Masking $service"
            systemctl mask "$service" 2>/dev/null || log "    WARNING: Failed to mask $service"
        else
            log "  $service already masked"
        fi
    else
        log "  $service not found, skipping"
    fi
done

# Create systemd-update-utmp.service override to change dependency
UTMP_SOURCE="/lib/systemd/system/systemd-update-utmp.service"
UTMP_OVERRIDE="/etc/systemd/system/systemd-update-utmp.service"

if [ -f "$UTMP_SOURCE" ]; then
    # Check if override already exists with the correct content
    if [ -f "$UTMP_OVERRIDE" ] && grep -q "multi-user.target" "$UTMP_OVERRIDE"; then
        log "  systemd-update-utmp.service override already configured"
    else
        log "  Creating systemd-update-utmp.service override"
        sed 's/sysinit\.target/multi-user.target/g' "$UTMP_SOURCE" > "$UTMP_OVERRIDE"
        systemctl daemon-reload
        log "  systemd-update-utmp.service override created"
    fi
else
    log "  systemd-update-utmp.service not found, skipping"
fi

# -----------------------------------------------------------------------------
# 7. Delay Anacron and Cron Services
# -----------------------------------------------------------------------------
log "Configuring anacron and cron service delays..."

# TODO: DOUBLE CHECK THIS ACTUALLY DOES STUFF

# Services to delay
SERVICES_TO_DELAY=("anacron.service" "cron.service")

for service in "${SERVICES_TO_DELAY[@]}"; do
    if systemctl list-unit-files "$service" &>/dev/null; then
        SERVICE_NAME="${service%%.*}"
        OVERRIDE_DIR="/etc/systemd/system/$service.d"
        OVERRIDE_FILE="$OVERRIDE_DIR/override.conf"
        
        mkdir -p "$OVERRIDE_DIR"
        
        if [ -f "$OVERRIDE_FILE" ] && grep -q "ExecStartPre=/bin/sleep 40" "$OVERRIDE_FILE"; then
            log "  $service delay already configured"
        else
            log "  Adding 40s delay to $service"
            cat > "$OVERRIDE_FILE" << 'EOF'
[Service]
ExecStartPre=/bin/sleep 40
EOF
            systemctl daemon-reload
            log "  $service delay configured"
        fi
    else
        log "  $service not found, skipping"
    fi
done



log "Hardware configuration completed"

# Exit with code 2 if reboot is required (allows caller to detect this)
if [ "$REBOOT_REQUIRED" = true ]; then
    exit 2
fi

exit 0
