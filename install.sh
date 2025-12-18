#!/bin/bash
#
# Innate OS Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/innate-inc/innate-os/main/install.sh | bash
#
# For private repositories:
#   GITHUB_TOKEN=ghp_xxx curl -fsSL -H "Authorization: token ghp_xxx" \
#     https://raw.githubusercontent.com/innate-inc/innate-os/main/install.sh | bash
#
# Options (environment variables):
#   GITHUB_TOKEN            - GitHub Personal Access Token (required for private repos)
#   BUILD_FROM_SOURCE=true  - Build from source instead of downloading pre-built release
#   INNATE_OS_DIR           - Installation directory (default: /home/$USER/innate-os)
#   GITHUB_REPO             - GitHub repository (default: innate-inc/innate-os)
#
# This script:
#   1. Checks system requirements (Ubuntu 22.04, amd64/arm64)
#   2. Sets up locale (en_US.UTF-8)
#   3. Installs prerequisites (curl, git, etc.)
#   4. Installs ROS2 Humble (GPG key, apt repository, ros-base)
#   5. Downloads latest release with pre-built artifacts (or builds from source)
#   6. Installs apt dependencies (from ros2_ws/apt-dependencies.txt)
#   7. Installs Python dependencies (from ros2_ws/pip-requirements.txt)
#   8. Installs Innate updater daemon (systemd service)
#   9. Installs system services (udev rules, helper scripts)
#  10. Configures shell environment
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
INNATE_OS_DIR="${INNATE_OS_DIR:-/home/$USER/innate-os}"
INNATE_STATE_DIR="${INNATE_STATE_DIR:-/var/lib/innate-update}"
GITHUB_REPO="${GITHUB_REPO:-innate-inc/innate-os}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

# Debug: show configuration
echo "[DEBUG] GITHUB_REPO=$GITHUB_REPO"
echo "[DEBUG] GITHUB_TOKEN=${GITHUB_TOKEN:+set (hidden)}"
echo "[DEBUG] INNATE_OS_DIR=$INNATE_OS_DIR"
ROS_DISTRO="humble"
# Set to "true" to build from source, "false" to download pre-built artifacts
BUILD_FROM_SOURCE="${BUILD_FROM_SOURCE:-false}"

# Helper for authenticated GitHub API/download requests
github_curl() {
    if [ -n "$GITHUB_TOKEN" ]; then
        curl -sSL -H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" "$@"
    else
        curl -sSL -H "Accept: application/vnd.github+json" "$@"
    fi
}

# Helper for downloading release assets (needs different Accept header)
github_download() {
    if [ -n "$GITHUB_TOKEN" ]; then
        curl -sSL -H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/octet-stream" "$@"
    else
        curl -sSL "$@"
    fi
}

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------

info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

success() {
    echo -e "${GREEN}[OK]${NC} $*"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

error() {
    echo -e "${RED}[ERROR]${NC} $*"
    exit 1
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# -----------------------------------------------------------------------------
# System checks
# -----------------------------------------------------------------------------

check_system() {
    info "Checking system requirements..."

    # Check Ubuntu version
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        if [ "$ID" != "ubuntu" ] || [ "$VERSION_ID" != "22.04" ]; then
            warn "This script is designed for Ubuntu 22.04. Detected: $ID $VERSION_ID"
            read -p "Continue anyway? (y/N) " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                exit 1
            fi
        fi
    else
        error "Cannot detect OS version"
    fi

    # Check architecture
    ARCH=$(dpkg --print-architecture)
    if [ "$ARCH" != "amd64" ] && [ "$ARCH" != "arm64" ]; then
        error "Unsupported architecture: $ARCH. Only amd64 and arm64 are supported."
    fi

    success "System check passed (Ubuntu $VERSION_ID, $ARCH)"
}

configure_needrestart() {
    info "Configuring needrestart for non-interactive mode..."

    # Configure needrestart to automatically restart services without prompting
    NEEDRESTART_CONF="/etc/needrestart/needrestart.conf"
    if [ -f "$NEEDRESTART_CONF" ]; then
        # Check if already configured
        if grep -q "^\$nrconf{restart} = 'a';" "$NEEDRESTART_CONF"; then
            success "needrestart already configured"
            return 0
        fi

        # Backup and update
        sudo cp "$NEEDRESTART_CONF" "${NEEDRESTART_CONF}.backup"

        # Update the restart config to automatic
        if grep -q '^\$nrconf{restart}' "$NEEDRESTART_CONF"; then
            sudo sed -i "s/^\$nrconf{restart}.*$/\$nrconf{restart} = 'a';/" "$NEEDRESTART_CONF"
        else
            echo "\$nrconf{restart} = 'a';" | sudo tee -a "$NEEDRESTART_CONF" > /dev/null
        fi

        success "needrestart configured for automatic restarts"
    else
        info "needrestart not installed, skipping configuration"
    fi
}

# -----------------------------------------------------------------------------
# ROS2 Installation
# -----------------------------------------------------------------------------

setup_locale() {
    info "Setting up locale..."

    # Check if locale is already configured
    if locale | grep -q "LANG=en_US.UTF-8"; then
        success "Locale already configured"
        return 0
    fi

    sudo apt-get update
    sudo apt-get install -y locales

    sudo locale-gen en_US en_US.UTF-8
    sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
    export LANG=en_US.UTF-8

    success "Locale configured"
}

install_prerequisites() {
    info "Installing prerequisites..."

    sudo apt-get update
    sudo apt-get install -y \
        software-properties-common \
        curl \
        gnupg \
        lsb-release \
        git \
        ca-certificates

    # Add universe repository (for gstreamer, pygame, etc.)
    sudo add-apt-repository universe -y

    success "Prerequisites installed"
}

install_ros2() {
    if [ -d "/opt/ros/$ROS_DISTRO" ]; then
        success "ROS2 $ROS_DISTRO is already installed"
        return 0
    fi

    info "Installing ROS2 $ROS_DISTRO..."

    # Add ROS2 GPG key
    sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
        -o /usr/share/keyrings/ros-archive-keyring.gpg

    # Add ROS2 repository
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" | \
        sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null

    # Install ROS2 base
    sudo apt-get update
    sudo apt-get install -y ros-humble-ros-base

    success "ROS2 $ROS_DISTRO installed"
}

# -----------------------------------------------------------------------------
# Clone/Download repository
# -----------------------------------------------------------------------------

save_github_token() {
    # Save token securely for future updates
    TOKEN_FILE="$INNATE_STATE_DIR/.github_token"
    mkdir -p "$INNATE_STATE_DIR"
    echo "$GITHUB_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    info "GitHub token saved for future updates"
}

load_github_token() {
    # Load saved token if not already set
    if [ -z "$GITHUB_TOKEN" ]; then
        TOKEN_FILE="$INNATE_STATE_DIR/.github_token"
        if [ -f "$TOKEN_FILE" ]; then
            GITHUB_TOKEN=$(cat "$TOKEN_FILE")
            export GITHUB_TOKEN
        fi
    fi
}

get_latest_release_info() {
    # Fetch latest release info from GitHub API
    RELEASE_API="https://api.github.com/repos/$GITHUB_REPO/releases/latest"
    # Note: info/warn go to stderr so they don't pollute the return value
    echo "[INFO] Checking releases at: $RELEASE_API" >&2
    RELEASE_INFO=$(github_curl "$RELEASE_API" 2>/dev/null)

    if [ -z "$RELEASE_INFO" ]; then
        echo "[WARN] Empty response from GitHub API" >&2
        return 1
    fi

    if echo "$RELEASE_INFO" | grep -q '"message".*"Not Found"'; then
        echo "[WARN] No releases found for $GITHUB_REPO" >&2
        return 1
    fi

    # Check for auth error
    if echo "$RELEASE_INFO" | grep -q "Bad credentials\|Requires authentication"; then
        echo "[WARN] GitHub authentication failed. Please check your GITHUB_TOKEN." >&2
        return 1
    fi

    # Extract tag name
    LATEST_TAG=$(echo "$RELEASE_INFO" | grep '"tag_name"' | head -1 | sed 's/.*"tag_name": "\([^"]*\)".*/\1/')

    # Extract asset URL - find the .tar.gz that's not the source archive
    # Get all browser_download_url lines, filter for .tar.gz but not -source.tar.gz
    ASSET_URL=$(echo "$RELEASE_INFO" | grep '"browser_download_url"' | grep -v '\-source\.tar\.gz' | head -1 | sed 's/.*"browser_download_url": "\([^"]*\)".*/\1/')

    if [ -z "$LATEST_TAG" ] || [ -z "$ASSET_URL" ]; then
        echo "[WARN] Could not extract release info (tag=$LATEST_TAG, url=$ASSET_URL)" >&2
        return 1
    fi

    # Only output the actual data to stdout
    echo "$LATEST_TAG|$ASSET_URL"
    return 0
}

download_release() {
    info "Fetching latest release info..."

    # Use || true to prevent set -e from exiting on failure
    RELEASE_DATA=$(get_latest_release_info) || true
    if [ -z "$RELEASE_DATA" ]; then
        warn "No releases found, falling back to git clone and build from source"
        clone_repository
        return 1  # Signal that we need to build
    fi

    LATEST_TAG=$(echo "$RELEASE_DATA" | cut -d'|' -f1)
    ASSET_URL=$(echo "$RELEASE_DATA" | cut -d'|' -f2)

    info "Latest release: $LATEST_TAG"
    info "Downloading pre-built release..."

    # Create parent directory
    mkdir -p "$(dirname "$INNATE_OS_DIR")"

    # Download and extract
    TEMP_ARCHIVE="/tmp/innate-os-release.tar.gz"
    if github_download -o "$TEMP_ARCHIVE" "$ASSET_URL"; then
        # Verify we got a valid archive (not an error page)
        if ! tar -tzf "$TEMP_ARCHIVE" >/dev/null 2>&1; then
            warn "Downloaded file is not a valid archive, falling back to git clone"
            rm -f "$TEMP_ARCHIVE"
            clone_repository
            return 0
        fi

        # Remove existing directory if present
        if [ -d "$INNATE_OS_DIR" ]; then
            warn "Removing existing installation at $INNATE_OS_DIR"
            rm -rf "$INNATE_OS_DIR"
        fi

        # Extract to parent directory (archive contains innate-os folder)
        tar -xzf "$TEMP_ARCHIVE" -C "$(dirname "$INNATE_OS_DIR")"
        rm -f "$TEMP_ARCHIVE"

        # Initialize git repo for future updates
        cd "$INNATE_OS_DIR"
        if [ ! -d ".git" ]; then
            info "Initializing git repository for future updates..."
            git init -q
            # Configure git remote with token if available
            if [ -n "$GITHUB_TOKEN" ]; then
                git remote add origin "https://${GITHUB_TOKEN}@github.com/$GITHUB_REPO.git"
            else
                git remote add origin "https://github.com/$GITHUB_REPO.git"
            fi
            git fetch --tags -q 2>/dev/null || true
            git checkout -b main -q 2>/dev/null || true
            git reset --soft "$LATEST_TAG" 2>/dev/null || true
        fi

        # Save the token for future updates (if provided)
        if [ -n "$GITHUB_TOKEN" ]; then
            save_github_token
        fi

        success "Release $LATEST_TAG downloaded and extracted"
    else
        warn "Failed to download release, falling back to git clone"
        clone_repository
        return 1  # Signal that we need to build
    fi
    return 0  # Signal that we have pre-built artifacts
}

clone_repository() {
    # Build git URL (with token for private repos)
    if [ -n "$GITHUB_TOKEN" ]; then
        GIT_URL="https://${GITHUB_TOKEN}@github.com/$GITHUB_REPO.git"
    else
        GIT_URL="https://github.com/$GITHUB_REPO.git"
    fi

    if [ -d "$INNATE_OS_DIR/.git" ]; then
        info "Repository already exists, updating..."
        cd "$INNATE_OS_DIR"

        # Update remote URL if token changed
        if [ -n "$GITHUB_TOKEN" ]; then
            git remote set-url origin "$GIT_URL" 2>/dev/null || true
        fi

        git fetch --all --tags
        # Checkout latest tag
        LATEST_TAG=$(git describe --tags --abbrev=0 origin/main 2>/dev/null || echo "")
        if [ -n "$LATEST_TAG" ]; then
            info "Checking out latest tag: $LATEST_TAG"
            git checkout "$LATEST_TAG"
        else
            git pull
        fi
        success "Repository updated"
    else
        info "Cloning Innate OS repository..."
        git clone "$GIT_URL" "$INNATE_OS_DIR"
        cd "$INNATE_OS_DIR"
        # Checkout latest tag if available
        LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
        if [ -n "$LATEST_TAG" ]; then
            info "Checking out latest tag: $LATEST_TAG"
            git checkout "$LATEST_TAG"
        fi
        success "Repository cloned to $INNATE_OS_DIR"
    fi

    # Save the token for future updates (if provided)
    if [ -n "$GITHUB_TOKEN" ]; then
        save_github_token
    fi
}

# -----------------------------------------------------------------------------
# Install dependencies
# -----------------------------------------------------------------------------

install_apt_dependencies() {
    info "Installing apt dependencies..."

    APT_DEPS_FILE="$INNATE_OS_DIR/ros2_ws/apt-dependencies.txt"

    if [ ! -f "$APT_DEPS_FILE" ]; then
        error "apt-dependencies.txt not found at $APT_DEPS_FILE"
    fi

    # Update package lists and install dependencies
    sudo apt-get update
    grep -v '^#' "$APT_DEPS_FILE" | grep -v '^$' | xargs sudo apt-get install -y

    success "Apt dependencies installed"
}

install_pip_dependencies() {
    info "Installing pip dependencies..."

    PIP_DEPS_FILE="$INNATE_OS_DIR/ros2_ws/pip-requirements.txt"

    if [ ! -f "$PIP_DEPS_FILE" ]; then
        error "pip-requirements.txt not found at $PIP_DEPS_FILE"
    fi

    pip3 install -r "$PIP_DEPS_FILE"

    success "Pip dependencies installed"
}

# -----------------------------------------------------------------------------
# Initialize rosdep
# -----------------------------------------------------------------------------

init_rosdep() {
    info "Initializing rosdep..."

    if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then
        sudo rosdep init || true
    fi
    rosdep update

    success "Rosdep initialized"
}

# -----------------------------------------------------------------------------
# Build ROS2 workspace
# -----------------------------------------------------------------------------

build_workspace() {
    info "Building ROS2 workspace..."

    cd "$INNATE_OS_DIR/ros2_ws"

    # Source ROS2
    source /opt/ros/$ROS_DISTRO/setup.bash

    # Install any remaining rosdep dependencies
    rosdep install --from-paths src --ignore-src -r -y || true

    # Build
    colcon build

    success "ROS2 workspace built"
}

# -----------------------------------------------------------------------------
# Install updater daemon
# -----------------------------------------------------------------------------

install_updater() {
    info "Installing Innate updater..."

    # Create state directory
    sudo mkdir -p "$INNATE_STATE_DIR"
    sudo chown $USER:$USER "$INNATE_STATE_DIR"

    # Copy updater script
    sudo cp "$INNATE_OS_DIR/scripts/update/innate-update" /usr/local/bin/innate-update
    sudo chmod +x /usr/local/bin/innate-update

    # Configure sudoers for passwordless post_update.sh execution
    SUDOERS_FILE="/etc/sudoers.d/innate-update"
    sudo tee "$SUDOERS_FILE" > /dev/null << EOF
# Allow $USER to run post_update.sh without password
$USER ALL=(ALL) NOPASSWD: $INNATE_OS_DIR/scripts/update/post_update.sh
EOF
    sudo chmod 440 "$SUDOERS_FILE"

    # Create systemd service for update daemon
    sudo tee /etc/systemd/system/innate-update.service > /dev/null << EOF
[Unit]
Description=Innate OS Update Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Environment="INNATE_OS_DIR=$INNATE_OS_DIR"
Environment="INNATE_STATE_DIR=$INNATE_STATE_DIR"
Environment="GITHUB_REPO=$GITHUB_REPO"
ExecStart=/usr/local/bin/innate-update daemon
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable innate-update.service

    success "Updater installed"
}

# -----------------------------------------------------------------------------
# Install system services
# -----------------------------------------------------------------------------

install_services() {
    info "Installing system services..."

    # Copy systemd service files
    if [ -d "$INNATE_OS_DIR/systemd" ]; then
        for service_file in "$INNATE_OS_DIR/systemd"/*.service; do
            if [ -f "$service_file" ]; then
                service_name=$(basename "$service_file")
                info "Installing $service_name"
                sudo cp "$service_file" /etc/systemd/system/
            fi
        done
        sudo systemctl daemon-reload
    fi

    # Copy udev rules
    if [ -d "$INNATE_OS_DIR/udev" ]; then
        for rule_file in "$INNATE_OS_DIR/udev"/*.rules; do
            if [ -f "$rule_file" ]; then
                rule_name=$(basename "$rule_file")
                info "Installing udev rule: $rule_name"
                sudo cp "$rule_file" /etc/udev/rules.d/
            fi
        done
        sudo udevadm control --reload-rules
        sudo udevadm trigger
    fi

    # Copy helper scripts
    if [ -f "$INNATE_OS_DIR/scripts/launch_ros_in_tmux.sh" ]; then
        sudo cp "$INNATE_OS_DIR/scripts/launch_ros_in_tmux.sh" /usr/local/bin/
        sudo chmod +x /usr/local/bin/launch_ros_in_tmux.sh
    fi

    success "System services installed"
}

# -----------------------------------------------------------------------------
# Setup shell environment
# -----------------------------------------------------------------------------

setup_shell() {
    info "Setting up shell environment..."

    # Detect shell config file
    if [ -f "$HOME/.zshrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    elif [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    else
        SHELL_RC="$HOME/.bashrc"
    fi

    # Check if already configured
    if grep -q "INNATE_OS" "$SHELL_RC" 2>/dev/null; then
        success "Shell already configured"
        return 0
    fi

    # Add environment setup
    cat >> "$SHELL_RC" << EOF

# ----- Innate OS Environment -----
export INNATE_OS_ROOT="$INNATE_OS_DIR"
source /opt/ros/$ROS_DISTRO/setup.bash
if [ -f "\$INNATE_OS_ROOT/ros2_ws/install/setup.bash" ]; then
    source "\$INNATE_OS_ROOT/ros2_ws/install/setup.bash"
fi
EOF

    success "Shell environment configured in $SHELL_RC"
}

# -----------------------------------------------------------------------------
# Print summary
# -----------------------------------------------------------------------------

print_summary() {
    echo
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Innate OS Installation Complete!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo
    echo "Installation directory: $INNATE_OS_DIR"
    echo
    echo "Next steps:"
    echo "  1. Reload your shell:  source ~/.bashrc  (or ~/.zshrc)"
    echo "  2. Start ROS nodes:    launch_ros_in_tmux.sh"
    echo "  3. Check for updates:  innate-update check"
    echo
    echo "Useful commands:"
    echo "  innate-update check   - Check for updates"
    echo "  innate-update apply   - Apply available update"
    echo "  innate-update status  - Show current version"
    echo
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    echo
    echo -e "${BLUE}============================================${NC}"
    echo -e "${BLUE}  Innate OS Installer${NC}"
    echo -e "${BLUE}============================================${NC}"
    echo

    check_system
    configure_needrestart
    setup_locale
    install_prerequisites
    install_ros2

    # Download pre-built release or clone and build from source
    NEED_BUILD=false

    if [ "$BUILD_FROM_SOURCE" = "true" ]; then
        info "Build from source mode enabled"
        clone_repository
        NEED_BUILD=true
    else
        # Try to download pre-built release (|| true to handle set -e)
        if download_release; then
            # Check if we actually got pre-built artifacts
            if [ -d "$INNATE_OS_DIR/ros2_ws/install" ]; then
                info "Pre-built artifacts found, skipping build"
            else
                NEED_BUILD=true
            fi
        else
            # download_release failed and fell back to git clone
            NEED_BUILD=true
        fi
    fi

    # Install dependencies (always needed)
    install_apt_dependencies
    install_pip_dependencies

    # Build if needed
    if [ "$NEED_BUILD" = "true" ]; then
        info "Building ROS2 workspace from source..."
        init_rosdep
        build_workspace
    fi

    install_updater
    install_services
    setup_shell
    print_summary
}

# Run main
main "$@"
