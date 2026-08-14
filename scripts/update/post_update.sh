#!/bin/bash
# Post-Update Script for Innate-OS
# This script runs after update/install to configure system components
# Requires root privileges via sudo
#
# Usage: sudo ./post_update.sh
#
# This script handles:
#   0a. Migrate root-level agents/, inputs/, skills/ into workspace/
#   0a2. Create config/settings.yaml from template if missing
#   0b. Enable a disk-backed swap file (INN-530)
#   0c. Use a headless (no-GUI) boot when no display is attached
#   0d. Disable desktop/update daemons with no robot function
#   1. Systemd service files
#   2. Helper scripts in /usr/local/bin
#   3. Udev rules
#   4. Bluetooth configuration
#   4b. Arducam microphone setup (ALSA)
#   5. Apt/pip dependencies
#   6. Rebuild ROS2 workspace
#   7. Zsh configuration
#   8. DDS setup
#   9. Sudoers configuration
#   10. Move primitives .h5 files to workspace/innate_skills/
#   11. Service restart

set -e  # Exit on error

# Print a warning banner on non-zero exit
SCRIPT_PATH=""
if command -v realpath >/dev/null 2>&1; then
    SCRIPT_PATH="$(realpath "$0")"
else
    SCRIPT_PATH="$0"
fi


# Parse arguments
SKIP_ROS_CLEAN=false
for arg in "$@"; do
    case $arg in
        --skip-ros-clean)
            SKIP_ROS_CLEAN=true
            ;;
    esac
done

# Check for root privileges
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root. Please use sudo." >&2
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
LOG_FILE="$REPO_DIR/logs/post_update.log"

# Get the actual user (not root)
ACTUAL_USER=${SUDO_USER:-$USER}
ACTUAL_HOME=$(eval echo ~$ACTUAL_USER)
ACTUAL_UID=$(id -u "$ACTUAL_USER")

# Create logs directory if it doesn't exist
mkdir -p "$(dirname "$LOG_FILE")"

# Logging function
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log_success() {
    local ts
    ts="$(date +'%Y-%m-%d %H:%M:%S')"
    echo -e "[$ts] \033[1;32m$*\033[0m"
    echo "[$ts] $*" >> "$LOG_FILE"
}

log_error() {
    local ts
    ts="$(date +'%Y-%m-%d %H:%M:%S')"
    echo -e "[$ts] \033[1;31m$*\033[0m"
    echo "[$ts] $*" >> "$LOG_FILE"
}

on_exit() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        log_error "============================================================"
        log_error "============================================================"
        log_error "UPDATE FAILED / PARTIALLY COMPLETED"
        log_error "Please re-run: sudo $SCRIPT_PATH"
        log_error "Or contact support for help."
        log_error "============================================================"
        log_error "============================================================"
        echo ""
    fi
}
trap on_exit EXIT

# Ensure log file has correct ownership
ensure_log_ownership() {
    if [ -f "$LOG_FILE" ]; then
        chown "$ACTUAL_USER:$ACTUAL_USER" "$LOG_FILE" 2>/dev/null || true
    fi
}

# DPkg::Lock::Timeout only covers the dpkg lock, not the lists lock that
# `apt-get update` takes, so apt fails instantly if e.g. the apt-daily timer
# is running. Retry until the lock frees up.
apt_update() {
    local attempt output
    for attempt in $(seq 1 60); do
        # Capture apt's output (appended to the log) for the lock check — only
        # lock contention is worth retrying. Echoed to stderr via fd 2 rather
        # than tee'd to /dev/stderr: under systemd fd 2 is a journald socket,
        # which tee cannot re-open (ENXIO), failing the pipeline even when apt
        # succeeded.
        if output=$(set -o pipefail; apt-get update 2>&1 | tee -a "$LOG_FILE"); then
            printf '%s\n' "$output" >&2
            return 0
        fi
        printf '%s\n' "$output" >&2
        grep -q "Could not get lock" <<<"$output" || return 1
        log "  apt lists lock busy (attempt $attempt/60), retrying in 5s..."
        sleep 5
    done
    return 1
}

log "========================================"
log "Starting post-update script"
log "Repository: $REPO_DIR"
log "User: $ACTUAL_USER"
log "========================================"

# Check hardware revision - R5 is no longer supported for updates beyond 0.1.0
R5_MAX_VERSION="0.1.1"
check_hardware_revision() {
    local robot_info_file="$REPO_DIR/data/robot_info.json"
    
    if [ -f "$robot_info_file" ]; then
        # Extract hardware_revision using grep and sed (no jq dependency)
        local hw_rev=$(grep -o '"hardware_revision"[[:space:]]*:[[:space:]]*"[^"]*"' "$robot_info_file" | sed 's/.*: *"\([^"]*\)".*/\1/')
        
        if [ "$hw_rev" = "R5" ]; then
            log "WARNING: Hardware revision R5 detected"
            log "R5 robots are limited to version $R5_MAX_VERSION"
            log "Reverting to $R5_MAX_VERSION..."
            
            # Checkout the max supported version for R5
            if git -C "$REPO_DIR" checkout "$R5_MAX_VERSION" 2>/dev/null; then
                log "Successfully reverted to $R5_MAX_VERSION"
            else
                log "ERROR: Failed to checkout $R5_MAX_VERSION"
            fi
            
            echo ""
            echo "╔════════════════════════════════════════════════════════════╗"
            echo "║  HARDWARE REVISION R5 - UPDATE NOT SUPPORTED               ║"
            echo "╠════════════════════════════════════════════════════════════╣"
            echo "║  This robot (R5) cannot receive updates beyond v$R5_MAX_VERSION.   ║"
            echo "║  The system has been kept at the last compatible version.  ║"
            echo "║  Please contact support for hardware upgrade options.      ║"
            echo "╚════════════════════════════════════════════════════════════╝"
            echo ""
            
            exit 0
        fi
    fi
}

check_hardware_revision

# Configure git for data integrity on unexpected shutdowns (e.g., power loss).
# core.fsync ensures git flushes writes to disk, preventing repository corruption.
# See: https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsync
log "Configuring git fsync settings..."
sudo -u "$ACTUAL_USER" git config --global core.fsync added,reference
sudo -u "$ACTUAL_USER" git config --global core.fsyncMethod fsync
log "Git fsync settings configured"

# Migrate git remote from old release repo to main repo
CURRENT_REMOTE=$(sudo -u "$ACTUAL_USER" git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)
if [ "$CURRENT_REMOTE" = "git@github.com:innate-inc/innate-os-release.git" ] || \
   [[ "$CURRENT_REMOTE" == https://github_pat_11AHZIPSQ0Z9*Bv3cH6uaC@github.com/innate-inc/innate-os.git ]]; then
    log "Migrating git remote from innate-os-release to innate-os..."
    sudo -u "$ACTUAL_USER" git -C "$REPO_DIR" remote set-url origin https://github.com/innate-inc/innate-os.git
    log "Git remote updated to https://github.com/innate-inc/innate-os.git"
fi

# -----------------------------------------------------------------------------
# 0. Migrate .env file (replace deprecated URLs with new endpoints)
# -----------------------------------------------------------------------------
ENV_FILE="$REPO_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    NEEDS_ENV_MIGRATION=false

    # Check for old/deprecated URL values
    if grep -qE '^INNATE_PROXY_URL=https://(robot-services|proxy-v1)\.innate\.bot' "$ENV_FILE" || \
       grep -qE '^BRAIN_WEBSOCKET_URI=wss://(brain|agent-v1)\.innate\.bot' "$ENV_FILE" || \
       grep -qE '^TELEMETRY_URL=https://(logs|logs-v1)\.innate\.bot' "$ENV_FILE"; then
        NEEDS_ENV_MIGRATION=true
    fi

    if [ "$NEEDS_ENV_MIGRATION" = true ]; then
        log "Migrating .env file (updating deprecated URLs to new endpoints)..."

        # Backup .env
        cp "$ENV_FILE" "$ACTUAL_HOME/.env.backup"
        chown "$ACTUAL_USER:$ACTUAL_USER" "$ACTUAL_HOME/.env.backup"
        log "  Backed up .env to $ACTUAL_HOME/.env.backup"

        # Replace deprecated URLs with their new equivalents in-place.
        # Both the oldest hosts and the -v1 generation migrate to the svc domains.
        sed -i 's|^INNATE_PROXY_URL=https://robot-services\.innate\.bot.*|INNATE_PROXY_URL=https://proxy-v1.svc.innate.bot|' "$ENV_FILE"
        sed -i 's|^BRAIN_WEBSOCKET_URI=wss://brain\.innate\.bot.*|BRAIN_WEBSOCKET_URI=wss://agent-v1.svc.innate.bot|' "$ENV_FILE"
        sed -i 's|^TELEMETRY_URL=https://logs\.innate\.bot.*|TELEMETRY_URL=https://logs.svc.innate.bot|' "$ENV_FILE"
        sed -i 's|^INNATE_PROXY_URL=https://proxy-v1\.innate\.bot.*|INNATE_PROXY_URL=https://proxy-v1.svc.innate.bot|' "$ENV_FILE"
        sed -i 's|^BRAIN_WEBSOCKET_URI=wss://agent-v1\.innate\.bot.*|BRAIN_WEBSOCKET_URI=wss://agent-v1.svc.innate.bot|' "$ENV_FILE"
        sed -i 's|^TELEMETRY_URL=https://logs-v1\.innate\.bot.*|TELEMETRY_URL=https://logs.svc.innate.bot|' "$ENV_FILE"

        # Add reference comment at the bottom of .env
        if ! grep -q 'Refer to .env.template for examples' "$ENV_FILE"; then
            echo "" >> "$ENV_FILE"
            echo "# Refer to .env.template for examples" >> "$ENV_FILE"
        fi

        chown "$ACTUAL_USER:$ACTUAL_USER" "$ENV_FILE"
        log "  Deprecated URLs replaced with new endpoints in .env"
        log "    robot-services / proxy-v1 → proxy-v1.svc.innate.bot"
        log "    brain / agent-v1          → agent-v1.svc.innate.bot"
        log "    logs / logs-v1            → logs.svc.innate.bot"
    fi
fi

# -----------------------------------------------------------------------------
# 0·1 Seed /etc/innate.env with the service key, so INNATE_SERVICE_KEY survives a repo
# reset that loses the .env. Readers use it only as a fallback (repo .env wins). Write-once:
# an existing key is left untouched. Mode 640 root:$ACTUAL_USER — root-owned but group-readable
# so the non-root launch readers can reach it.
# -----------------------------------------------------------------------------
SYSTEM_ENV_FILE="/etc/innate.env"
if [ -f "$ENV_FILE" ]; then
    # Require a non-empty value (.+) so the empty template default never gets copied.
    SERVICE_KEY_LINE=$(grep -E '^INNATE_SERVICE_KEY=.+' "$ENV_FILE" | tail -n1 || true)
    if [ -n "$SERVICE_KEY_LINE" ]; then
        if [ -f "$SYSTEM_ENV_FILE" ] && grep -qE '^INNATE_SERVICE_KEY=.+' "$SYSTEM_ENV_FILE"; then
            log "Service key already present in $SYSTEM_ENV_FILE (leaving its contents untouched)"
        else
            (umask 077; printf '%s\n' "$SERVICE_KEY_LINE" > "$SYSTEM_ENV_FILE")
            log "Copied INNATE_SERVICE_KEY from .env to $SYSTEM_ENV_FILE"
        fi
    fi
fi

# Repair the mode outside the seeding branch above: a robot whose .env lost its key — the
# very case this fallback exists for — or one flashed with a pre-seeded /etc/innate.env
# never reaches that branch, so gating the repair on it leaves the file 0600 root:root,
# unreadable by the non-root launch readers (print_runtime_env.py then treats it as
# absent and the service key silently drops out — proxy "not configured").
# Idempotent; matches the seeded state above. Contents are never touched.
if [ -f "$SYSTEM_ENV_FILE" ]; then
    if [ "$ACTUAL_USER" = "root" ]; then
        # Run from a root shell, no sudo: "root:root 640" would revoke the launch
        # readers' group access on a file that may currently be correct.
        log "Skipping $SYSTEM_ENV_FILE permission repair (no non-root user; re-run via sudo)"
    elif [ "$(stat -c '%U:%G %a' "$SYSTEM_ENV_FILE")" != "root:$ACTUAL_USER 640" ]; then
        # Best-effort under set -e: a user without a same-named group would fail
        # the chown, and that must not abort the rest of the update.
        if chown "root:$ACTUAL_USER" "$SYSTEM_ENV_FILE" && chmod 640 "$SYSTEM_ENV_FILE"; then
            log "Set $SYSTEM_ENV_FILE to 640 root:$ACTUAL_USER so launch readers can read the service key"
        else
            log "WARNING: could not fix $SYSTEM_ENV_FILE ownership/mode (group '$ACTUAL_USER' missing?); continuing"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 0a. Migrate user-created data into the post-refactor layout.
# The refactor moved agents/skills/inputs under workspace/ and maps + nav-state
# (.last_mode/.last_map) under data/. git checkout relocated the *tracked*
# shipped files; this step preserves any user-created *untracked* content
# (custom skills/agents/inputs, trained models, SLAM maps, last mode/map).
# Idempotent, never overwrites, only rmdir's empty dirs.
# Home-dir ~/agents and ~/skills are migrated too: 0.7 loads only from
# workspace/, so leaving them in place would silently stop loading them.
# -----------------------------------------------------------------------------
# shellcheck source=scripts/update/migrate_user_data.sh
source "$SCRIPT_DIR/migrate_user_data.sh"
MIGRATE_CHOWN_USER="$ACTUAL_USER" MIGRATE_HOME="$ACTUAL_HOME" run_user_data_migrations "$REPO_DIR"

# -----------------------------------------------------------------------------
# 0a2. Create config/settings.yaml from template if missing.
# settings.yaml = tunable ROS parameters (the file users edit). Only created when missing,
# so an existing robot's config is never overwritten on upgrade.
# -----------------------------------------------------------------------------
SETTINGS_CONFIG="$REPO_DIR/config/settings.yaml"
SETTINGS_TEMPLATE="$REPO_DIR/config/settings.yaml.template"

if [ ! -f "$SETTINGS_CONFIG" ] && [ -f "$SETTINGS_TEMPLATE" ]; then
    log "Creating config/settings.yaml from template..."
    cp "$SETTINGS_TEMPLATE" "$SETTINGS_CONFIG"
    chown "$ACTUAL_USER:$ACTUAL_USER" "$SETTINGS_CONFIG"
    log "  Created $SETTINGS_CONFIG (uncomment a stanza to tune a parameter)"
fi

# -----------------------------------------------------------------------------
# 0b. Enable a disk-backed swap file (INN-530)
# Jetson ships only zram (compressed RAM swap), which adds no real headroom once
# physical RAM is exhausted — the cause of OOM kills during memory-heavy work
# like the colcon rebuild further down. A small NVMe-backed swap file adds a
# genuine overflow tier; the kernel fills high-priority zram first, then spills
# here. Enabled before the build so this very update run already benefits.
# Idempotent: created/enabled once, then persisted in /etc/fstab.
# Non-fatal: swap must never block an update, so failures only warn.
# -----------------------------------------------------------------------------
SWAP_FILE="/swapfile"
SWAP_SIZE_GB=8

setup_swap() {
    if swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$SWAP_FILE"; then
        # Already active (the common case on every update after the first).
        # Still fall through to the fstab check below: an earlier run interrupted
        # between swapon and the fstab append leaves swap active but unpersisted.
        log "  Swap file already active ($SWAP_FILE)"
    else
        # Not active. Any existing file may be a partial leftover from an
        # interrupted run (power loss, disk-full, OOM-killed dd); a truncated
        # file would make mkswap fail on every future update. So never trust an
        # existing file: rebuild from scratch, and remove it on any failure so a
        # botched run can't permanently block swap setup on later updates.
        rm -f "$SWAP_FILE"
        log "  Creating ${SWAP_SIZE_GB}G swap file at $SWAP_FILE..."

        # Create with mode 600 up front (never world-readable), then allocate.
        # fallocate works on the ext4 NVMe root; fall back to dd where unsupported.
        install -m 600 /dev/null "$SWAP_FILE" || return 1
        if ! fallocate -l "${SWAP_SIZE_GB}G" "$SWAP_FILE"; then
            log "  fallocate unavailable; falling back to dd"
            dd if=/dev/zero of="$SWAP_FILE" bs=1M count=$((SWAP_SIZE_GB * 1024)) status=none \
                || { rm -f "$SWAP_FILE"; return 1; }
        fi

        if ! mkswap "$SWAP_FILE" >/dev/null || ! swapon "$SWAP_FILE"; then
            rm -f "$SWAP_FILE"
            return 1
        fi
        log "  Swap enabled ($SWAP_FILE)"
    fi

    # Persist across reboots. nofail keeps boot clean if the file is ever
    # missing (e.g. removed by a failed re-setup); the entry then just waits
    # for the next successful run instead of producing a degraded swap unit.
    if ! grep -qE "^${SWAP_FILE}[[:space:]]" /etc/fstab; then
        # Guard against an fstab without a trailing newline: a bare append
        # would glue the swap entry onto the previous mount line.
        [ -n "$(tail -c1 /etc/fstab)" ] && echo >> /etc/fstab
        echo "$SWAP_FILE none swap sw,nofail 0 0" >> /etc/fstab
        log "  Added $SWAP_FILE to /etc/fstab"
    fi
    return 0
}

log "Configuring swap..."
if setup_swap; then
    log "  Swap configuration complete"
else
    log "  WARNING: swap configuration failed (continuing without disk swap)"
fi

# -----------------------------------------------------------------------------
# 0c. Headless (no-GUI) boot.
# The stock Ubuntu desktop image boots into GNOME (~0.8 GB RAM) that nothing
# renders on the robot. Switch the default target to multi-user; the GUI stays
# one `systemctl set-default graphical.target` away, and takes effect on the
# next boot.
# -----------------------------------------------------------------------------
log "Configuring boot target..."
if systemctl set-default multi-user.target >/dev/null 2>&1; then
    log "  Set headless (multi-user) boot for next boot"
else
    log "  WARNING: failed to set headless boot target (continuing)"
fi

# -----------------------------------------------------------------------------
# 0d. Disable desktop/update daemons with no robot function.
# These ship with the desktop image and only cost RAM + periodic CPU wakeups on
# a headless robot. Absent units are ignored (non-fatal); snapd only when no
# snaps are installed. Reversible via `systemctl enable --now <unit>`.
# -----------------------------------------------------------------------------
disable_unit() {
    # Units absent on this image error individually; the rest are still
    # disabled and stopped. Non-fatal — errors surface as warnings.
    systemctl disable --now "$@" 2>&1 >/dev/null | while IFS= read -r line; do
        log "  WARNING: $line"
    done
}

# fwupd is intentionally left enabled — it's the channel for LVFS firmware
# updates, the one disabled daemon that removes a real capability.
log "Disabling desktop/update daemons with no robot function..."
disable_unit \
    packagekit.service \
    ModemManager.service \
    cups.service cups.socket cups-browsed.service lpd.service \
    colord.service \
    switcheroo-control.service \
    kerneloops.service \
    ubuntu-advantage.service \
    apt-daily.timer apt-daily-upgrade.timer \
    unattended-upgrades.service

# snapd: only disable when nothing is installed as a snap (else we'd break it).
# Installed snaps are checked on the filesystem, not via `snap list` — the snap
# CLI socket-activates snapd and waits for it to load state, hanging 30s+.
if command -v snap >/dev/null 2>&1 && ls /var/lib/snapd/snaps/*.snap >/dev/null 2>&1; then
    log "  Keeping snapd (installed snaps present)"
else
    disable_unit snapd.service snapd.socket
fi

# Stop running services before updating (keep app.cpp alive during build)
log "Stopping services to begin update..."

# Stop systemd services first (if they exist)
for service in jetson-perf.service zenoh-router.service ros-app.service ble-provisioner.service speaker-keepalive.service; do
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        log "Stopping $service"
        systemctl stop "$service"
    fi
done

# Gracefully stop tmux panes - keep app.cpp alive during build
if sudo -u "$ACTUAL_USER" tmux has-session -t ros_nodes 2>/dev/null; then
    log "Stopping ROS nodes (keeping app.cpp alive during build)..."
    
    # Kill all windows except app-bringup
    for window in "arm-recorder" "brain-nav" "behaviors-inputs" "stream" "ik-logger"; do
        if sudo -u "$ACTUAL_USER" tmux list-windows -t ros_nodes 2>/dev/null | grep -q "$window"; then
            log "  Stopping window: $window"
            sudo -u "$ACTUAL_USER" tmux kill-window -t "ros_nodes:$window" 2>/dev/null || true
        fi
    done
    
    log "Other nodes stopped. App and bringup still running for status updates."
else
    log "No ros_nodes tmux session found"
fi

log "Services stopped (app.cpp still running)."

# -----------------------------------------------------------------------------
# 1. Update systemd service files
# -----------------------------------------------------------------------------
log "Installing systemd service files..."
if [ -d "$REPO_DIR/config/systemd" ]; then
    for service_file in "$REPO_DIR/config/systemd"/*.service; do
        if [ -f "$service_file" ]; then
            service_name=$(basename "$service_file")
            log "  Installing $service_name"

            # The repo units are written against the reference install and templated
            # here. The tokens a new unit must spell exactly to be rewritten: `jetson1`
            # as the user, `/home/jetson1/innate-os` as the checkout, `/home/jetson1` as
            # the home, `/run/user/1000` as the runtime dir. A unit that invents its own
            # spelling silently ships the reference values.
            # The checkout rule has to run before the bare-home rule, or the checkout
            # becomes $ACTUAL_HOME/innate-os and stops matching the paths the sudoers
            # file pins to $REPO_DIR.
            # Remove any existing symlink to avoid truncating source file
            rm -f /etc/systemd/system/"$service_name"
            sed -e "s|/home/jetson1/innate-os|$REPO_DIR|g" \
                -e "s|/home/jetson1|$ACTUAL_HOME|g" \
                -e "s/User=jetson1/User=$ACTUAL_USER/g" \
                -e "s/sudo -u jetson1/sudo -u $ACTUAL_USER/g" \
                -e "s|/run/user/1000|/run/user/$ACTUAL_UID|g" \
                "$service_file" > /etc/systemd/system/"$service_name"
        fi
    done
    systemctl daemon-reload
    log "Systemd daemon reloaded"
fi

# -----------------------------------------------------------------------------
# 1b. Install security limits (real-time scheduling for the ROS runtime)
# -----------------------------------------------------------------------------
if [ -d "$REPO_DIR/config/security/limits.d" ]; then
    log "Installing security limits..."
    for limits_file in "$REPO_DIR/config/security/limits.d"/*.conf; do
        if [ -f "$limits_file" ]; then
            limits_name=$(basename "$limits_file")
            log "  Installing $limits_name"
            sed "s/jetson1/$ACTUAL_USER/g" \
                "$limits_file" > /etc/security/limits.d/"$limits_name"
        fi
    done
fi

# -----------------------------------------------------------------------------
# 2. Update helper scripts in /usr/local/bin
# -----------------------------------------------------------------------------
log "Installing helper scripts..."
if [ -d "$REPO_DIR/scripts" ]; then
    # Symlink innate-update (so git pull automatically updates the command)
    if [ -f "$REPO_DIR/scripts/update/innate-update" ]; then
        log "  Symlinking innate-update"
        rm -f /usr/local/bin/innate-update
        ln -s "$REPO_DIR/scripts/update/innate-update" /usr/local/bin/innate-update
    fi

    # Symlink innate dev CLI
    if [ -f "$REPO_DIR/scripts/innate" ]; then
        log "  Symlinking innate"
        rm -f /usr/local/bin/innate
        ln -s "$REPO_DIR/scripts/innate" /usr/local/bin/innate
    fi

    # Regenerate zsh completions from the Click command tree
    if [ -f "$REPO_DIR/scripts/innate" ]; then
        log "  Generating zsh completions"
        # Clean up stale completions from old path (was root-owned, blocks colcon rebuild)
        rm -rf "$REPO_DIR/ros2_ws/build/completions"
        mkdir -p "$REPO_DIR/scripts/build/completions"
        chown -R "$ACTUAL_USER:$ACTUAL_USER" "$REPO_DIR/scripts/build"
        sudo -u "$ACTUAL_USER" "$REPO_DIR/scripts/innate" completions > "$REPO_DIR/scripts/build/completions/_innate"
    fi

    # Symlink restart script if it exists
    if [ -f "$REPO_DIR/scripts/restart_robot_networking.sh" ]; then
        log "  Symlinking restart_robot_networking.sh"
        rm -f /usr/local/bin/restart_robot_networking.sh
        ln -s "$REPO_DIR/scripts/restart_robot_networking.sh" /usr/local/bin/restart_robot_networking.sh
    fi

    # Symlink tmux launcher if it exists
    if [ -f "$REPO_DIR/scripts/launch_ros_in_tmux.sh" ]; then
        log "  Symlinking launch_ros_in_tmux.sh"
        rm -f /usr/local/bin/launch_ros_in_tmux.sh
        ln -s "$REPO_DIR/scripts/launch_ros_in_tmux.sh" /usr/local/bin/launch_ros_in_tmux.sh
    fi

    # Copy zsh history fixer if it exists
    if [ -f "$REPO_DIR/scripts/fix-zsh-history.sh" ]; then
        log "  Installing fix-zsh-history.sh"
        cp "$REPO_DIR/scripts/fix-zsh-history.sh" /usr/local/bin/
        chmod +x /usr/local/bin/fix-zsh-history.sh
    fi
fi

# -----------------------------------------------------------------------------
# 3. Update udev rules
# -----------------------------------------------------------------------------
log "Installing udev rules..."
if [ -d "$REPO_DIR/config/udev" ]; then
    for rule_file in "$REPO_DIR/config/udev"/*.rules; do
        if [ -f "$rule_file" ]; then
            rule_name=$(basename "$rule_file")
            log "  Installing $rule_name"
            rm -f /etc/udev/rules.d/"$rule_name"
            cp "$rule_file" /etc/udev/rules.d/
        fi
    done
    udevadm control --reload-rules
    udevadm trigger
    log "Udev rules reloaded"
fi

# -----------------------------------------------------------------------------
# 3b. Install SSH keepalive drop-in
# Keeps VS Code Remote-SSH (and plain ssh) tunnels alive across Wi-Fi sleep and
# NAT idle-timeouts so they are not silently dropped — which, with password
# auth, would force a reconnect and password re-entry. Only sets keepalive,
# never touches auth. Validated before applying and reloaded (not restarted) so
# existing connections are never dropped; backs out cleanly if validation fails.
# -----------------------------------------------------------------------------
log "Installing SSH keepalive configuration..."
SSH_DROPIN_SRC="$REPO_DIR/config/ssh/10-innate-fleet.conf"
SSH_DROPIN_DST="/etc/ssh/sshd_config.d/10-innate-fleet.conf"
if [ -f "$SSH_DROPIN_SRC" ] && [ -d "$(dirname "$SSH_DROPIN_DST")" ]; then
    if [ -f "$SSH_DROPIN_DST" ] && diff -q "$SSH_DROPIN_SRC" "$SSH_DROPIN_DST" >/dev/null 2>&1; then
        log "  SSH keepalive config already up to date"
    else
        cp "$SSH_DROPIN_SRC" "$SSH_DROPIN_DST"
        chmod 644 "$SSH_DROPIN_DST"
        # Validate the full sshd config; back out the drop-in if invalid so we
        # never leave a broken config that could block sshd on its next restart.
        if SSHD_VALIDATE_ERR=$(sshd -t 2>&1); then
            if systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null; then
                log "  SSH keepalive config installed and ssh reloaded"
            else
                log "  SSH keepalive config installed (reload skipped; applies on next ssh restart)"
            fi
        else
            log "  WARNING: sshd config validation failed; removing keepalive drop-in"
            log "  sshd -t output: $SSHD_VALIDATE_ERR"
            rm -f "$SSH_DROPIN_DST"
        fi
    fi
else
    log "  Skipping SSH keepalive config (source or sshd_config.d not present)"
fi

# 4. Configure passwordless shutdown for ROS app
log "Configuring passwordless shutdown..."
ACTUAL_USER=${SUDO_USER:-$USER}
SUDOERS_FILE="/etc/sudoers.d/ros-shutdown"
echo "$ACTUAL_USER ALL=(ALL) NOPASSWD: /sbin/shutdown" > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
log "Passwordless shutdown configured for user $ACTUAL_USER"


# -----------------------------------------------------------------------------
# 5. Update Bluetooth configurations (optional - only on systems with bluetooth)
# -----------------------------------------------------------------------------
log "Configuring hardware..."

HARDWARE_REBOOT_REQUIRED=false
HARDWARE_SCRIPT="$REPO_DIR/scripts/update/configure_hardware.sh"
if [ -f "$HARDWARE_SCRIPT" ]; then
    chmod +x "$HARDWARE_SCRIPT"
    "$HARDWARE_SCRIPT" "$REPO_DIR" 2>&1 | tee -a "$LOG_FILE"
    HARDWARE_EXIT_CODE=${PIPESTATUS[0]}
    if [ $HARDWARE_EXIT_CODE -eq 0 ]; then
        log "  Hardware configuration completed successfully"
    elif [ $HARDWARE_EXIT_CODE -eq 3 ]; then
        log "  Hardware configuration completed (reboot required)"
        HARDWARE_REBOOT_REQUIRED=true
    else
        log "  WARNING: Hardware configuration script failed"
    fi
else
    log "  WARNING: configure_hardware.sh not found at $HARDWARE_SCRIPT"
fi

# -----------------------------------------------------------------------------
# 6. Install/update dependencies
# -----------------------------------------------------------------------------
    # Apt dependencies
    log "Checking apt dependencies..."
    
    # Check if ROS 2 Humble is installed
    if ! dpkg -l | grep -q "^ii.*ros-humble-ros-base"; then
        log "  ROS 2 Humble not found. Installing ROS 2 Humble base..."
        
        # Add ROS2 GPG key
        if [ ! -f /usr/share/keyrings/ros-archive-keyring.gpg ]; then
            log "    Adding ROS 2 GPG key..."
            curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
                -o /usr/share/keyrings/ros-archive-keyring.gpg || {
                log "    ERROR: Failed to download ROS 2 GPG key"
                exit 1
            }
        fi
        
        # Add ROS2 repository
        if [ ! -f /etc/apt/sources.list.d/ros2.list ]; then
            log "    Adding ROS 2 repository..."
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" | \
                tee /etc/apt/sources.list.d/ros2.list > /dev/null
        fi
        
        # Install ROS2 base
        log "    Updating package lists..."
        apt_update || {
            log "    ERROR: Failed to update package lists"
            exit 1
        }
        log "    Installing ROS 2 Humble base..."
        apt-get -o DPkg::Lock::Timeout=45 install -y ros-humble-ros-base || {
            log "    ERROR: Failed to install ROS 2 Humble base"
            exit 1
        }
        log "  ROS 2 Humble base installed"
    else
        log "  ROS 2 Humble base already installed"
    fi
    
    # Configure third-party apt repositories (Innate packages).
    # shellcheck source=scripts/update/setup_repos.sh
    source "$SCRIPT_DIR/setup_repos.sh"
    setup_package_repos

    APT_DEPS_COMMON="$REPO_DIR/ros2_ws/apt-dependencies.common.txt"
    APT_DEPS_HARDWARE="$REPO_DIR/ros2_ws/apt-dependencies.hardware.txt"

    # Add git-core PPA for latest git version (if not already added)
    if ! grep -q "git-core/ppa" /etc/apt/sources.list.d/*.list 2>/dev/null; then
        log "  Adding git-core PPA..."
        add-apt-repository -y --no-update ppa:git-core/ppa
    fi

    apt_update || {
        log "  ERROR: Failed to update package lists"
        exit 1
    }

    # Install all apt dependencies (common + hardware-specific) in one go
    if [ -f "$APT_DEPS_COMMON" ] && [ -f "$APT_DEPS_HARDWARE" ]; then
        log "  Installing apt dependencies (common + hardware)..."
        cat "$APT_DEPS_COMMON" "$APT_DEPS_HARDWARE" | grep -v '^#' | grep -v '^$' | xargs apt-get -o DPkg::Lock::Timeout=45 install -y
        log "  Apt dependencies installed"

        # Refresh the linker cache so freshly installed libs are discoverable. In
        # particular libnvdla_compiler.so (from nvidia-l4t-dla-compiler) lands in
        # /usr/lib/aarch64-linux-gnu/nvidia -- that path is in ld.so.conf.d but the
        # cache can be stale, and libnvinfer dlopens it at `import tensorrt`. Without
        # this, TensorRT can't import and the ACT policy fails to load.
        ldconfig
        log "  Refreshed linker cache (ldconfig)"
    fi

    # Remove PulseAudio (conflicts with ALSA-only audio setup)
    if dpkg -l | grep -q "^ii.*pulseaudio "; then
        log "Removing PulseAudio..."
        apt-get -o DPkg::Lock::Timeout=45 purge -y pulseaudio pulseaudio-utils 2>/dev/null || true
        apt-get -o DPkg::Lock::Timeout=45 autoremove -y 2>/dev/null || true
        log "  PulseAudio removed"
    else
        log "  PulseAudio not installed, skipping removal"
    fi

    # Pip dependencies
    log "Checking Python dependencies..."
    PIP_DEPS_FILE="$REPO_DIR/ros2_ws/pip-requirements.txt"
    if [ -f "$PIP_DEPS_FILE" ]; then
        # Uninstall mediapipe to avoid protobuf dependency conflict
        # mediapipe requires protobuf<5, but we need protobuf>=6 for other packages
        if sudo -u "$ACTUAL_USER" pip3 show mediapipe &>/dev/null; then
            log "  Uninstalling mediapipe (protobuf conflict)..."
            sudo -u "$ACTUAL_USER" pip3 uninstall -y mediapipe 2>/dev/null || true
            log "  Mediapipe uninstalled"
        else
            log "  Mediapipe not installed, skipping uninstall"
        fi
        
        # Install Jetson-optimized PyTorch separately with ONLY the Jetson index
        # pip prefers manylinux wheels over platform-specific wheels, so we CANNOT
        # have PyPI as a fallback when installing torch or it will grab the CPU wheel
        #
        # The URL MUST end in /+simple/ -- that is the PEP 503 index devpi serves.
        # Without it pip hits the browsable web UI, which serves no wheel links
        # (torch included) and 404s outright for anything the index doesn't host
        # itself (sympy, networkx, jinja2, pillow...), so the install dies. On
        # /+simple/ devpi proxies PyPI for those while still shadowing torch,
        # torchvision and torchaudio with the Jetson aarch64 wheels.
        JETSON_INDEX="https://pypi.jetson-ai-lab.io/jp6/cu126/+simple/"
        TORCH_VERSION="2.8.0"
        TORCHVISION_VERSION="0.23.0"
        # Pin torchaudio too: the index also carries 2.9.x/2.10.x builds whose
        # metadata only requires "torch" (no version bound), so an unpinned
        # torchaudio would pair 2.10 with torch 2.8 and fail at import time.
        TORCHAUDIO_VERSION="2.8.0"
        
        CUDA_AVAILABLE=$(sudo -u "$ACTUAL_USER" python3 -c "import torch; print(torch.cuda.is_available())" 2>/dev/null || echo "False")
        if [[ "$CUDA_AVAILABLE" != "True" ]]; then
            log "  PyTorch CUDA not available, reinstalling from Jetson AI Lab..."
            sudo -u "$ACTUAL_USER" pip3 uninstall -y torch torchvision torchaudio 2>/dev/null || true
            sudo -u "$ACTUAL_USER" pip3 install --no-cache-dir \
                "torch==$TORCH_VERSION" \
                "torchvision==$TORCHVISION_VERSION" \
                "torchaudio==$TORCHAUDIO_VERSION" \
                --index-url "$JETSON_INDEX"
            log "  PyTorch installed from Jetson AI Lab"
        fi
        
        # Install remaining pip dependencies (torch already installed above)
        log "  Installing other pip dependencies..."
        sudo -u "$ACTUAL_USER" pip3 install -r "$PIP_DEPS_FILE" --upgrade-strategy only-if-needed
        log "  Pip dependencies installed"
    fi

# -----------------------------------------------------------------------------
# 6b. Rebuild ROS2 workspace if needed
# -----------------------------------------------------------------------------
log "Checking ROS2 workspace..."
if [ -d "$REPO_DIR/ros2_ws/src" ]; then
    log "Rebuilding ROS2 workspace..."
    cd "$REPO_DIR/ros2_ws"

    # Run as the actual user, not root
    if [ "$SKIP_ROS_CLEAN" = true ]; then
        log "  Skipping clean build (--skip-ros-clean)"
        sudo -u "$ACTUAL_USER" bash -c "cd $REPO_DIR/ros2_ws && source /opt/ros/humble/setup.bash && colcon build"
    else
        sudo -u "$ACTUAL_USER" bash -c "cd $REPO_DIR/ros2_ws && source /opt/ros/humble/setup.bash && rm -rf build/ install/ log/ && colcon build"
    fi

    if [ $? -eq 0 ]; then
        log "ROS2 workspace rebuilt successfully"
    else
        log "ERROR: Failed to rebuild ROS2 workspace"
        exit 1
    fi
fi

# Now kill the remaining app.cpp pane (build is done, safe to stop it)
if sudo -u "$ACTUAL_USER" tmux has-session -t ros_nodes 2>/dev/null; then
    log "Build complete. Stopping app.cpp..."
    sudo -u "$ACTUAL_USER" tmux kill-session -t ros_nodes 2>/dev/null || true
fi

# -----------------------------------------------------------------------------
# 8. Setup Zsh configuration
# -----------------------------------------------------------------------------
log "Setting up Zsh configuration..."

# Install oh-my-zsh if not present
if [ ! -d "$ACTUAL_HOME/.oh-my-zsh" ]; then
    log "  Installing Oh My Zsh..."
    sudo -u "$ACTUAL_USER" sh -c 'RUNZSH=no CHSH=no sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"' || true
fi

# Backup existing .zshrc if it exists and isn't ours
if [ -f "$ACTUAL_HOME/.zshrc" ]; then
    if ! grep -q "INNATE_OS_ROOT" "$ACTUAL_HOME/.zshrc"; then
        log "  Backing up existing .zshrc"
        cp "$ACTUAL_HOME/.zshrc" "$ACTUAL_HOME/.zshrc.backup.$(date +%s)"
    fi
fi

# Copy our zsh configuration files
if [ -d "$REPO_DIR/config/zsh" ]; then
    log "  Installing Innate zsh configuration"

    # Copy .zshrc.pre-oh-my-zsh (contains ROS2/DDS setup)
    if [ -f "$REPO_DIR/config/zsh/.zshrc.pre-oh-my-zsh" ]; then
        # Update paths in the file
        sed -e "s|/home/jetson1|$ACTUAL_HOME|g" \
            "$REPO_DIR/config/zsh/.zshrc.pre-oh-my-zsh" > "$ACTUAL_HOME/.zshrc.pre-oh-my-zsh"
        chown "$ACTUAL_USER:$ACTUAL_USER" "$ACTUAL_HOME/.zshrc.pre-oh-my-zsh"
    fi

    # Copy main .zshrc
    if [ -f "$REPO_DIR/config/zsh/.zshrc" ]; then
        # Update paths in the file
        sed -e "s|/home/jetson1|$ACTUAL_HOME|g" \
            "$REPO_DIR/config/zsh/.zshrc" > "$ACTUAL_HOME/.zshrc"
        chown "$ACTUAL_USER:$ACTUAL_USER" "$ACTUAL_HOME/.zshrc"
    fi
fi

# Ensure INNATE_OS_ROOT is set correctly in .zshrc
if [ -f "$ACTUAL_HOME/.zshrc" ]; then
    if ! grep -q "export INNATE_OS_ROOT=" "$ACTUAL_HOME/.zshrc"; then
        log "  Adding INNATE_OS_ROOT to .zshrc"
        echo "" >> "$ACTUAL_HOME/.zshrc"
        echo "export INNATE_OS_ROOT=\"$REPO_DIR\"" >> "$ACTUAL_HOME/.zshrc"
    else
        # Update existing INNATE_OS_ROOT
        sed -i "s|export INNATE_OS_ROOT=.*|export INNATE_OS_ROOT=\"$REPO_DIR\"|g" "$ACTUAL_HOME/.zshrc"
    fi
fi

# Set zsh as default shell if not already
if [ "$(getent passwd $ACTUAL_USER | cut -d: -f7)" != "/bin/zsh" ] && [ -x /bin/zsh ]; then
    log "  Setting zsh as default shell for $ACTUAL_USER"
    chsh -s /bin/zsh "$ACTUAL_USER" || true
fi

# Add user to dialout and i2c groups for serial port and I2C access
if ! groups "$ACTUAL_USER" | grep -q "\bdialout\b" || ! groups "$ACTUAL_USER" | grep -q "\bi2c\b"; then
    log "  Adding $ACTUAL_USER to dialout and i2c groups for serial port and I2C access"
    usermod -aG dialout,i2c "$ACTUAL_USER" || true
    log "  Note: User may need to log out and back in for group changes to take effect"
fi

# Suppress default Ubuntu MOTD on SSH login (our custom banner in .zshrc is enough)
HUSHLOGIN="$ACTUAL_HOME/.hushlogin"
if [ ! -f "$HUSHLOGIN" ]; then
    log "  Creating .hushlogin to suppress Ubuntu MOTD"
    touch "$HUSHLOGIN"
    chown "$ACTUAL_USER:$ACTUAL_USER" "$HUSHLOGIN"
fi

# -----------------------------------------------------------------------------
# 9. Setup DDS configuration
# -----------------------------------------------------------------------------
log "Setting up DDS configuration..."
if [ -d "$REPO_DIR/config/dds" ]; then
    # Ensure DDS scripts are executable
    chmod +x "$REPO_DIR/config/dds"/*.zsh 2>/dev/null || true

    # Generate initial DDS config (will be regenerated on shell login)
    if [ -f "$REPO_DIR/config/dds/setup_dds.zsh" ]; then
        log "  DDS setup script ready at $REPO_DIR/config/dds/setup_dds.zsh"
    fi
fi

# -----------------------------------------------------------------------------
# 10. Configure sudoers for passwordless restart script
# -----------------------------------------------------------------------------
log "Configuring sudoers..."

# Allow user to run restart_robot_networking.sh without password
SUDOERS_FILE="/etc/sudoers.d/innate-os"
cat > "$SUDOERS_FILE" << EOF
# Innate-OS sudoers configuration
# Allow $ACTUAL_USER to run specific scripts without password

# Restart robot networking (called by BLE provisioner)
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/local/bin/restart_robot_networking.sh

# Identity applier: --write from the BLE provisioner (--seed runs as root, from its unit).
$ACTUAL_USER ALL=(ALL) NOPASSWD: $REPO_DIR/scripts/identity/innate-identity

# Reboot after BLE provisioning writes an identity (the reply goes out first).
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/sbin/reboot
$ACTUAL_USER ALL=(ALL) NOPASSWD: /sbin/reboot

# Post-update script (called by innate-update)
$ACTUAL_USER ALL=(ALL) NOPASSWD: $REPO_DIR/scripts/update/post_update.sh

# NetworkManager CLI (called by BLE provisioner for WiFi configuration)
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli
$ACTUAL_USER ALL=(ALL) NOPASSWD: /bin/nmcli

# Disk SMART health, read by innate_logger. Unrestricted on purpose: pinning
# the arguments limited the sweep to a fixed device list, and this account
# already has NOPASSWD on post_update.sh, which it owns.
# Backticks are command substitution in this unquoted heredoc -- never use them here.
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/sbin/smartctl

# systemctl for ROS node management (called by innate CLI)
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ros-app.service
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop ros-app.service
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ros-app.service
EOF
chmod 440 "$SUDOERS_FILE"
log "  Sudoers configured for $ACTUAL_USER"

# -----------------------------------------------------------------------------
# 11. (User-data migration — primitives models, legacy directives, maps and
# nav-state — now runs up front in step 0a via run_user_data_migrations.)
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 11b. Download skill assets from metadata.json
# -----------------------------------------------------------------------------
for meta_file in "$REPO_DIR"/workspace/innate_skills/*/metadata.json "$REPO_DIR"/workspace/custom_skills/*/metadata.json; do
    [ -f "$meta_file" ] || continue
    jq -r '.downloads // {} | to_entries[] | "\(.key)\t\(.value)"' "$meta_file" 2>/dev/null | \
    while IFS=$'\t' read -r fname url; do
        dest="$(dirname "$meta_file")/$fname"
        [ -f "$dest" ] && continue
        log "  Downloading $dest"
        sudo -u "$ACTUAL_USER" curl -fsSL -o "$dest.tmp" "$url"
        mv "$dest.tmp" "$dest"
    done
done

# -----------------------------------------------------------------------------
# 12. Enable and restart services
# -----------------------------------------------------------------------------
log "Enabling and starting services..."

# List of services to enable/start
SERVICES=("jetson-perf.service" "zenoh-router.service" "ros-app.service")

# Add ble-provisioner if the service file exists
if [ -f "/etc/systemd/system/ble-provisioner.service" ]; then
    SERVICES+=("ble-provisioner.service")
fi

# Add arducam-audio if the service file exists
if [ -f "/etc/systemd/system/arducam-audio.service" ]; then
    SERVICES+=("arducam-audio.service")
fi

# Add speaker-keepalive if the service file exists
if [ -f "/etc/systemd/system/speaker-keepalive.service" ]; then
    SERVICES+=("speaker-keepalive.service")
fi

# Add shutdown-sound if the service file exists (enable only, runs at shutdown)
if [ -f "/etc/systemd/system/shutdown-sound.service" ]; then
    SERVICES+=("shutdown-sound.service")
fi

# Add the 443->4443 port redirect if the service file exists (lets the webapp be
# reached at https://<robot>.local with no :4443). In RESTART_SERVICES below so
# an update re-applies the rule live instead of waiting for a reboot.
if [ -f "/etc/systemd/system/innate-port-redirect.service" ]; then
    SERVICES+=("innate-port-redirect.service")
fi

# Add bluetooth if available
if systemctl list-unit-files bluetooth.service &>/dev/null; then
    SERVICES+=("bluetooth.service")
fi

# zenoh-router and ros-app must be *restarted* (not just started) after a
# rebuild: `systemctl start` is a no-op when they're already running, which
# leaves the long-lived Zenoh router holding stale liveliness tokens from the
# previous node generation and the old node binaries in place — surfacing as
# `TypeHashNotSupported` drops and behavior-goal-acceptance timeouts. Restart
# the router before the nodes so the fresh graph comes up against a fresh router.
RESTART_SERVICES=("zenoh-router.service" "ros-app.service" "innate-port-redirect.service")

# Always enable services (so they start on boot after reboot)
# But only start/restart them if reboot is not required
for service in "${SERVICES[@]}"; do
    if [ -f "/etc/systemd/system/$service" ] || systemctl list-unit-files "$service" &>/dev/null; then
        log "  Enabling $service"
        systemctl enable "$service" 2>/dev/null || true

        if [ "$HARDWARE_REBOOT_REQUIRED" = true ]; then
            log "  Skipping start of $service (reboot required - will start after reboot)"
        elif [[ " ${RESTART_SERVICES[*]} " == *" $service "* ]]; then
            log "  Restarting $service"
            systemctl restart "$service" 2>/dev/null || true
        else
            log "  Starting $service"
            systemctl start "$service" 2>/dev/null || true
        fi
    fi
done

# -----------------------------------------------------------------------------
# 13. Launch ROS nodes in Tmux (optional, depends on ros-app.service)
# -----------------------------------------------------------------------------
# Note: If ros-app.service is configured correctly, it will launch the tmux session
# If not using systemd, you can manually launch:
# sudo -u "$ACTUAL_USER" INNATE_OS_ROOT="$REPO_DIR" bash "$REPO_DIR/scripts/launch_ros_in_tmux.sh" &

log_success "========================================"
log_success "Post-update script completed successfully"
log_success "========================================"

if [ "$HARDWARE_REBOOT_REQUIRED" != true ]; then
    log ""
    log "To start ROS manually:"
    log "  tmux attach -t ros_nodes  (if already running)"
    log "  OR"
    log "  launch_ros_in_tmux.sh     (to start fresh)"
    log ""
fi

# Notify if reboot is required for hardware changes
if [ "$HARDWARE_REBOOT_REQUIRED" = true ]; then
    log ""
    log "╔════════════════════════════════════════════════════════════╗"
    log "║  REBOOT REQUIRED                                           ║"
    log "║  Hardware configuration changes require a system reboot.   ║"
    log "╚════════════════════════════════════════════════════════════╝"
    log ""
fi

# Fix log file ownership
ensure_log_ownership

exit 0

