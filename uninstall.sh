#!/bin/bash
#
# Innate OS Uninstaller
# Usage: curl -fsSL https://raw.githubusercontent.com/.../uninstall.sh | bash
#    or: ./uninstall.sh
#
# Options:
#   --keep-ros    Don't uninstall ROS2 (useful if you have other ROS projects)
#   --keep-deps   Don't uninstall apt/pip dependencies
#   -y            Skip confirmation prompts
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
INNATE_OS_DIR="${INNATE_OS_DIR:-$HOME/innate-os}"
INNATE_STATE_DIR="${INNATE_STATE_DIR:-/var/lib/innate-update}"

# Parse arguments
KEEP_ROS=false
KEEP_DEPS=false
SKIP_CONFIRM=false

for arg in "$@"; do
    case $arg in
        --keep-ros) KEEP_ROS=true ;;
        --keep-deps) KEEP_DEPS=true ;;
        -y|--yes) SKIP_CONFIRM=true ;;
    esac
done

info() { echo -e "${BLUE}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

echo
echo -e "${RED}============================================${NC}"
echo -e "${RED}  Innate OS Uninstaller${NC}"
echo -e "${RED}============================================${NC}"
echo

echo "This will remove:"
echo "  - Innate OS installation at $INNATE_OS_DIR"
echo "  - State directory at $INNATE_STATE_DIR"
echo "  - Systemd services (innate-update, ros-app, discovery-server)"
echo "  - Udev rules"
echo "  - Shell configuration"
echo "  - Scripts in /usr/local/bin"
if [ "$KEEP_ROS" = false ]; then
    echo "  - ROS2 Humble"
fi
echo

if [ "$SKIP_CONFIRM" = false ]; then
    read -p "Are you sure you want to continue? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# Stop services
info "Stopping services..."
sudo systemctl stop innate-update.service 2>/dev/null || true
sudo systemctl stop ros-app.service 2>/dev/null || true
sudo systemctl stop discovery-server.service 2>/dev/null || true

# Kill tmux session if running
if tmux has-session -t ros_nodes 2>/dev/null; then
    info "Killing tmux session ros_nodes..."
    tmux kill-session -t ros_nodes 2>/dev/null || true
fi

# Disable and remove systemd services
info "Removing systemd services..."
sudo systemctl disable innate-update.service 2>/dev/null || true
sudo systemctl disable ros-app.service 2>/dev/null || true
sudo systemctl disable discovery-server.service 2>/dev/null || true

sudo rm -f /etc/systemd/system/innate-update.service
sudo rm -f /etc/systemd/system/ros-app.service
sudo rm -f /etc/systemd/system/discovery-server.service
sudo systemctl daemon-reload

# Remove udev rules
info "Removing udev rules..."
sudo rm -f /etc/udev/rules.d/*innate*.rules 2>/dev/null || true
sudo rm -f /etc/udev/rules.d/*maurice*.rules 2>/dev/null || true
sudo udevadm control --reload-rules 2>/dev/null || true

# Remove scripts from /usr/local/bin
info "Removing scripts from /usr/local/bin..."
sudo rm -f /usr/local/bin/innate-update
sudo rm -f /usr/local/bin/launch_ros_in_tmux.sh
sudo rm -f /usr/local/bin/restart_robot_networking.sh

# Remove sudoers file
info "Removing sudoers configuration..."
sudo rm -f /etc/sudoers.d/innate-update

# Remove state directory
info "Removing state directory..."
sudo rm -rf "$INNATE_STATE_DIR"

# Remove installation directory
info "Removing installation directory..."
if [ -d "$INNATE_OS_DIR" ]; then
    rm -rf "$INNATE_OS_DIR"
    success "Removed $INNATE_OS_DIR"
else
    warn "Installation directory not found: $INNATE_OS_DIR"
fi

# Clean shell configuration
info "Cleaning shell configuration..."
for RC_FILE in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$RC_FILE" ]; then
        # Remove Innate OS block from shell config
        if grep -q "INNATE_OS" "$RC_FILE"; then
            # Create backup
            cp "$RC_FILE" "${RC_FILE}.backup"
            # Remove the Innate OS section
            sed -i '/# ----- Innate OS Environment -----/,/^$/d' "$RC_FILE" 2>/dev/null || \
            sed -i '' '/# ----- Innate OS Environment -----/,/^$/d' "$RC_FILE" 2>/dev/null || true
            success "Cleaned $RC_FILE (backup at ${RC_FILE}.backup)"
        fi
    fi
done

# Optionally remove ROS2
if [ "$KEEP_ROS" = false ]; then
    info "Removing ROS2 Humble..."
    sudo apt-get remove -y 'ros-humble-*' 2>/dev/null || true
    sudo apt-get autoremove -y 2>/dev/null || true
    sudo rm -f /etc/apt/sources.list.d/ros2.list
    sudo rm -f /usr/share/keyrings/ros-archive-keyring.gpg
    success "ROS2 removed"
else
    info "Keeping ROS2 (--keep-ros specified)"
fi

echo
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Innate OS Uninstalled${NC}"
echo -e "${GREEN}============================================${NC}"
echo
echo "To complete cleanup, you may want to:"
echo "  1. Reload your shell: source ~/.bashrc (or ~/.zshrc)"
echo "  2. Remove any Docker images: docker rmi innate-os"
echo
