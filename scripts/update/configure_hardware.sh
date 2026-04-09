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
#   - Arducam microphone setup (ALSA)
#   - WiFi power management (disable power saving for stable ROS/DDS)
#   - Ethernet connection for SSH access (static IP, non-default route)

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
# 1b. ALSA dmix configuration (software mixing)
# -----------------------------------------------------------------------------
log "Configuring ALSA dmix..."

ALSA_CONF_SRC="$REPO_DIR/config/alsa/asound.conf"
ALSA_CONF_DST="/etc/asound.conf"

if [ -f "$ALSA_CONF_SRC" ]; then
    if [ -f "$ALSA_CONF_DST" ] && diff -q "$ALSA_CONF_SRC" "$ALSA_CONF_DST" >/dev/null 2>&1; then
        log "  ALSA dmix + softvol already up to date"
    else
        log "  Installing ALSA dmix config (enables concurrent audio playback)..."
        cp "$ALSA_CONF_SRC" "$ALSA_CONF_DST"
        log "  ALSA dmix configured"
    fi
else
    log "  WARNING: asound.conf not found at $ALSA_CONF_SRC"
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
# 3. Arducam Microphone (ALSA)
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
# 5. Ethernet Connection for SSH Access
# -----------------------------------------------------------------------------
log "Configuring Ethernet connection for SSH access..."

ETH_CONNECTION="jetson-eth"
ETH_INTERFACE="enP8p1s0"
ETH_IP="192.168.50.2/24"

# Bail safely if this Jetson doesn't have the expected interface name
if ! ip link show "$ETH_INTERFACE" >/dev/null 2>&1; then
    log "  WARNING: Interface $ETH_INTERFACE not found; skipping Ethernet profile"
else
    # Make sure link is up (safe even if unplugged)
    nmcli dev set "$ETH_INTERFACE" managed yes 2>/dev/null || true
    ip link set "$ETH_INTERFACE" up 2>/dev/null || true

    if nmcli con show "$ETH_CONNECTION" &>/dev/null; then
        log "  Ethernet connection '$ETH_CONNECTION' already exists, updating..."
        nmcli con modify "$ETH_CONNECTION" \
            connection.interface-name "$ETH_INTERFACE" \
            connection.autoconnect yes \
            connection.autoconnect-priority 10 \
            ipv4.method manual \
            ipv4.addresses "$ETH_IP" \
            ipv4.never-default yes \
            ipv4.dns "" \
            ipv4.ignore-auto-dns yes
    else
        log "  Creating Ethernet connection '$ETH_CONNECTION'..."
        nmcli con add type ethernet \
            ifname "$ETH_INTERFACE" \
            con-name "$ETH_CONNECTION" \
            connection.autoconnect yes \
            connection.autoconnect-priority 10 \
            ipv4.method manual \
            ipv4.addresses "$ETH_IP" \
            ipv4.never-default yes \
            ipv4.dns "" \
            ipv4.ignore-auto-dns yes
    fi

    log "  Activating Ethernet connection..."
    nmcli con up "$ETH_CONNECTION" || log "  Ethernet not plugged in yet — profile will auto-activate later"

    log "  Ethernet configuration completed"
fi

# -----------------------------------------------------------------------------
# 6. Sudo Permissions for Hostname Control
# -----------------------------------------------------------------------------
log "Configuring sudo permissions for hostname control..."

SUDOERS_FILE="/etc/sudoers.d/99-jetson1-hostnamectl"

# Create sudoers file to allow jetson1 to run hostnamectl and avahi-daemon restart without password
cat > "$SUDOERS_FILE" << 'EOF'
# Allow jetson1 user to set hostname and restart avahi-daemon without password
jetson1 ALL=(ALL) NOPASSWD: /usr/bin/hostnamectl *
jetson1 ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart avahi-daemon.service
EOF

# Set proper permissions for sudoers file (must be 0440)
chmod 0440 "$SUDOERS_FILE"
log "  Created $SUDOERS_FILE with proper permissions"

# Verify the sudoers file is valid
if visudo -c -f "$SUDOERS_FILE" >/dev/null 2>&1; then
    log "  Sudoers file validated successfully"
else
    log "  ERROR: Invalid sudoers file, removing for safety"
    rm -f "$SUDOERS_FILE"
fi

log "Hardware configuration completed"

# Exit with code 2 if reboot is required (allows caller to detect this)
if [ "$REBOOT_REQUIRED" = true ]; then
    exit 3
fi

exit 0
