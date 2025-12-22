#!/bin/bash
# Install deploy key on a robot and configure git remote
# Usage: ./install-key-on-robot.sh <robot-number> <robot-user@robot-ip>
#
# Example: ./install-key-on-robot.sh 1 jetson1@192.168.55.1
#          ./install-key-on-robot.sh 42 jetson1@192.168.1.100

set -e

# Configuration
RELEASE_REPO="${RELEASE_REPO:-innate-inc/innate-os-release}"
INNATE_OS_PATH="${INNATE_OS_PATH:-/home/jetson1/innate-os}"
DEPLOY_KEYS_DIR="${DEPLOY_KEYS_DIR:-$(cd "$(dirname "$0")/../../deploy-keys" && pwd)}"
PREFIX="robot"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <robot-number> [robot-user@robot-ip]"
    echo "Example: $0 1"
    echo "         $0 1 jetson1@192.168.55.1"
    exit 1
fi

ROBOT_NUM="$1"
ROBOT_HOST="${2:-jetson1@192.168.55.1}"

# Format robot ID with zero-padding (1 -> robot-001)
ROBOT_ID=$(printf '%s-%03d' "$PREFIX" "$ROBOT_NUM")
KEY_FILE="$DEPLOY_KEYS_DIR/$ROBOT_ID/innate_deploy_key"

if [ ! -f "$KEY_FILE" ]; then
    echo "Error: Robot #$ROBOT_NUM not found"
    echo "       Looked for: $KEY_FILE"
    echo ""
    echo "Available robots:"
    ls -d "$DEPLOY_KEYS_DIR"/*/ 2>/dev/null | xargs -n1 basename | sort || echo "  (none)"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Installing deploy key for $ROBOT_ID"
echo "  Target: $ROBOT_HOST"
echo "  Release repo: $RELEASE_REPO"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Copy private key
echo "1. Copying deploy key..."
scp "$KEY_FILE" "$ROBOT_HOST:~/innate_deploy_key.tmp"

# Setup on robot (use bash explicitly to avoid zsh issues)
echo "2. Configuring SSH and git remote..."
ssh "$ROBOT_HOST" bash << REMOTE_EOF
set -e
RELEASE_REPO="$RELEASE_REPO"
INNATE_OS_PATH="$INNATE_OS_PATH"
ROBOT_ID="$ROBOT_ID"

# Kill any running tmux sessions (ros_nodes)
if tmux has-session -t ros_nodes 2>/dev/null; then
    tmux kill-session -t ros_nodes
    echo "   ✓ Killed ros_nodes tmux session"
fi

# Remove old SSH keys (no longer needed with deploy keys)
if [ -f ~/.ssh/id_ed25519 ]; then
    rm -f ~/.ssh/id_ed25519 ~/.ssh/id_ed25519.pub
    echo "   ✓ Removed old SSH keys (id_ed25519)"
fi
if [ -f ~/.ssh/id_rsa ]; then
    rm -f ~/.ssh/id_rsa ~/.ssh/id_rsa.pub
    echo "   ✓ Removed old SSH keys (id_rsa)"
fi

# Remove git user config (name and email)
git config --global --unset user.name 2>/dev/null || true
git config --global --unset user.email 2>/dev/null || true
echo "   ✓ Removed git user config (name and email)"

# Install deploy key
mkdir -p ~/.ssh
mv ~/innate_deploy_key.tmp ~/.ssh/innate_deploy_key
chmod 600 ~/.ssh/innate_deploy_key
echo "   ✓ Deploy key installed"

# Add GitHub to known_hosts if not present
if ! grep -q "github.com" ~/.ssh/known_hosts 2>/dev/null; then
    echo "   Adding GitHub to known_hosts..."
    ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null || true
    chmod 644 ~/.ssh/known_hosts
    echo "   ✓ GitHub added to known_hosts"
fi

# Add/update SSH config to ensure it uses the correct deploy key for this robot
mkdir -p ~/.ssh
touch ~/.ssh/config
chmod 600 ~/.ssh/config

# Remove existing github.com entry if present (to ensure we use the correct key)
if grep -q "^Host github.com" ~/.ssh/config 2>/dev/null; then
    # Create a temporary file without the github.com section
    # Remove from "Host github.com" to the next "Host" line or end of file
    awk '
        /^Host github.com$/ { skip=1; next }
        /^Host / && skip { skip=0 }
        !skip { print }
    ' ~/.ssh/config > ~/.ssh/config.tmp 2>/dev/null && mv ~/.ssh/config.tmp ~/.ssh/config || true
fi

# Always add the correct SSH config for this robot's deploy key
echo "" >> ~/.ssh/config
echo "# Innate OS deploy key for \$ROBOT_ID" >> ~/.ssh/config
echo "Host github.com" >> ~/.ssh/config
echo "    HostName github.com" >> ~/.ssh/config
echo "    User git" >> ~/.ssh/config
echo "    IdentityFile ~/.ssh/innate_deploy_key" >> ~/.ssh/config
echo "    IdentitiesOnly yes" >> ~/.ssh/config
echo "   ✓ SSH config updated for \$ROBOT_ID"

# Clone or update git remote to release repo
if [ ! -d "\$INNATE_OS_PATH/.git" ]; then
    # Remove existing directory if it exists but isn't a git repo
    if [ -d "\$INNATE_OS_PATH" ]; then
        echo "   Removing existing innate-os directory..."
        rm -rf "\$INNATE_OS_PATH"
    fi
    # Clone the repository
    echo "   Cloning innate-os repository..."
    mkdir -p "\$(dirname "\$INNATE_OS_PATH")"
    git clone "git@github.com:\$RELEASE_REPO.git" "\$INNATE_OS_PATH" || {
        echo "   ❌ Failed to clone repository"
        echo "     Please ensure the deploy key has access to \$RELEASE_REPO"
        exit 1
    }
    echo "   ✓ Cloned innate-os repository"
    cd "\$INNATE_OS_PATH"
else
    cd "\$INNATE_OS_PATH"
    
    # Preserve .env file if it exists (before git operations that might overwrite it)
    ENV_BACKUP=""
    if [ -f "\$INNATE_OS_PATH/.env" ]; then
        ENV_BACKUP=\$(cat "\$INNATE_OS_PATH/.env")
        echo "   ✓ Preserved existing .env file"
    fi
    
    # Ensure maps/.gitignore exists before git operations
    if [ ! -f "\$INNATE_OS_PATH/maps/.gitignore" ]; then
        mkdir -p "\$INNATE_OS_PATH/maps"
        echo "*.pgm" > "\$INNATE_OS_PATH/maps/.gitignore"
        echo "*.yaml" >> "\$INNATE_OS_PATH/maps/.gitignore"
        echo "" >> "\$INNATE_OS_PATH/maps/.gitignore"
        echo "   ✓ Restored maps/.gitignore"
    fi
    
    # Switch to main branch
    git checkout main 2>/dev/null || git checkout -b main
    echo "   ✓ Switched to main branch"
    
    # Delete all other local branches (handle empty case)
    BRANCHES_TO_DELETE=\$(git branch | grep -v '^\* main\$' | grep -v '^  main\$' | grep -v '^$' || true)
    if [ -n "\$BRANCHES_TO_DELETE" ]; then
        echo "\$BRANCHES_TO_DELETE" | while read branch; do
            [ -n "\$branch" ] && git branch -D "\$branch" 2>/dev/null && echo "   ✓ Deleted branch: \$branch" || true
        done
    fi
    
    # Update remote to release repo
    git remote set-url origin "git@github.com:\$RELEASE_REPO.git"
    echo "   ✓ Git remote set to git@github.com:\$RELEASE_REPO.git"
    
    # Prune remote tracking branches
    git remote prune origin 2>/dev/null || true
    git fetch --prune 2>/dev/null || true
    echo "   ✓ Pruned stale remote branches"
    
    # Reset to match remote exactly (discard any local commits/changes)
    # This ensures the robot always matches the release repo state
    if git rev-parse --verify origin/main >/dev/null 2>&1; then
        git reset --hard origin/main 2>/dev/null || {
            echo "   ⚠️  Warning: Failed to reset to origin/main, attempting pull..."
            git pull origin main 2>/dev/null || git pull 2>/dev/null || true
        }
        echo "   ✓ Reset to match origin/main"
    else
        # Fallback to pull if origin/main doesn't exist yet
        git pull origin main 2>/dev/null || git pull 2>/dev/null || true
        echo "   ✓ Pulled latest from release repo"
    fi
    
    # Restore .env file if it was backed up and was overwritten by git operations
    if [ -n "\$ENV_BACKUP" ]; then
        if [ ! -f "\$INNATE_OS_PATH/.env" ] || ! grep -q "INNATE_SERVICE_KEY=" "\$INNATE_OS_PATH/.env" 2>/dev/null; then
            echo "\$ENV_BACKUP" > "\$INNATE_OS_PATH/.env"
            echo "   ✓ Restored .env file from backup"
        fi
    fi
fi

# Test GitHub connection
echo ""
echo "3. Testing GitHub connection..."
ssh -T git@github.com 2>&1 | head -1 || true

# Final report
echo ""
echo "4. Final report..."
echo ""
echo "   SSH keys in ~/.ssh:"
ls -la ~/.ssh/*.pub ~/.ssh/innate_deploy_key 2>/dev/null | awk '{print "     " \$NF}' || echo "     (none)"
echo ""
echo "   Git remote:"
cd "\$INNATE_OS_PATH" 2>/dev/null && git remote -v | head -2 | awk '{print "     " \$0}' || echo "     (not configured)"
echo ""
echo "   .env file:"
if [ -f "\$INNATE_OS_PATH/.env" ]; then
    cat "\$INNATE_OS_PATH/.env" | awk '{print "     " \$0}'
else
    echo "     (no .env file found)"
fi
REMOTE_EOF

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✓ Setup complete for $ROBOT_ID"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "The robot can now pull updates:"
echo "  ssh $ROBOT_HOST 'cd $INNATE_OS_PATH && git pull'"
