#!/bin/bash
# Install deploy key on a robot and configure git remote
# Usage: ./install-key-on-robot.sh <robot-id> <robot-user@robot-ip>
#
# Example: ./install-key-on-robot.sh robot-001 jetson1@192.168.1.100

set -e

# Configuration (set during key generation)
RELEASE_REPO="innate-inc/innate-os-release"
INNATE_OS_PATH="/home/jetson1/innate-os"

if [ $# -ne 2 ]; then
    echo "Usage: $0 <robot-id> <robot-user@robot-ip>"
    echo "Example: $0 robot-001 jetson1@192.168.1.100"
    exit 1
fi

ROBOT_ID="$1"
ROBOT_HOST="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KEY_DIR="$SCRIPT_DIR/$ROBOT_ID"

if [ ! -d "$KEY_DIR" ]; then
    echo "Error: Robot directory not found: $KEY_DIR"
    echo "Available robots:"
    ls -d "$SCRIPT_DIR"/*/  2>/dev/null | xargs -n1 basename
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
scp "$KEY_DIR/innate_deploy_key" "$ROBOT_HOST:~/innate_deploy_key.tmp"

# Setup on robot (use bash explicitly to avoid zsh issues)
echo "2. Configuring SSH and git remote..."
ssh "$ROBOT_HOST" bash << REMOTE_EOF
set -e
RELEASE_REPO="$RELEASE_REPO"
INNATE_OS_PATH="$INNATE_OS_PATH"

# Install deploy key
mkdir -p ~/.ssh
mv ~/innate_deploy_key.tmp ~/.ssh/innate_deploy_key
chmod 600 ~/.ssh/innate_deploy_key
echo "   ✓ Deploy key installed"

# Add SSH config if not present
if ! grep -q "innate_deploy_key" ~/.ssh/config 2>/dev/null; then
    cat >> ~/.ssh/config << 'SSHCONFIG'

# Innate OS deploy key
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/innate_deploy_key
    IdentitiesOnly yes
SSHCONFIG
    chmod 600 ~/.ssh/config
    echo "   ✓ SSH config updated"
else
    echo "   ✓ SSH config already configured"
fi

# Update git remote to release repo
if [ -d "$INNATE_OS_PATH/.git" ]; then
    cd "$INNATE_OS_PATH"
    
    # Switch to main branch
    git checkout main 2>/dev/null || git checkout -b main
    echo "   ✓ Switched to main branch"
    
    # Delete all other local branches
    git branch | grep -v '^\* main\$' | grep -v '^  main\$' | while read branch; do
        git branch -D "\$branch" 2>/dev/null && echo "   ✓ Deleted branch: \$branch"
    done
    
    # Update remote to release repo
    git remote set-url origin "git@github.com:\$RELEASE_REPO.git"
    echo "   ✓ Git remote set to git@github.com:$RELEASE_REPO.git"
    
    # Prune remote tracking branches
    git remote prune origin 2>/dev/null || true
    git fetch --prune 2>/dev/null || true
    echo "   ✓ Pruned stale remote branches"
else
    echo "   ⚠ innate-os not found at $INNATE_OS_PATH"
    echo "     Clone it first with: git clone git@github.com:$RELEASE_REPO.git $INNATE_OS_PATH"
fi

# Test GitHub connection
echo ""
echo "3. Testing GitHub connection..."
ssh -T git@github.com 2>&1 | head -1 || true
REMOTE_EOF

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✓ Setup complete for $ROBOT_ID"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "The robot can now pull updates:"
echo "  ssh $ROBOT_HOST 'cd /home/jetson1/innate-os && git pull'"
