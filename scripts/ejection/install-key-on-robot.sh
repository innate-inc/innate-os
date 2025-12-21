#!/bin/bash
# Install deploy key on a robot and configure git remote
# Usage: ./install-key-on-robot.sh <key-file> <robot-user@robot-ip>
#
# Example: ./install-key-on-robot.sh ./deploy-keys/robot-001/innate_deploy_key jetson1@192.168.55.1

set -e

# Configuration
RELEASE_REPO="${RELEASE_REPO:-innate-inc/innate-os-release}"
INNATE_OS_PATH="${INNATE_OS_PATH:-/home/jetson1/innate-os}"

if [ $# -lt 2 ]; then
    echo "Usage: $0 <key-file> <robot-user@robot-ip>"
    echo "Example: $0 ./deploy-keys/robot-001/innate_deploy_key jetson1@192.168.55.1"
    echo ""
    echo "Environment variables:"
    echo "  RELEASE_REPO     Release repository (default: innate-inc/innate-os-release)"
    echo "  INNATE_OS_PATH   Path to innate-os on robot (default: /home/jetson1/innate-os)"
    exit 1
fi

KEY_FILE="$1"
ROBOT_HOST="$2"

if [ ! -f "$KEY_FILE" ]; then
    echo "Error: Key file not found: $KEY_FILE"
    exit 1
fi

ROBOT_ID=$(basename "$(dirname "$KEY_FILE")")

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

# Remove old SSH keys (no longer needed with deploy keys)
if [ -f ~/.ssh/id_ed25519 ]; then
    rm -f ~/.ssh/id_ed25519 ~/.ssh/id_ed25519.pub
    echo "   ✓ Removed old SSH keys (id_ed25519)"
fi
if [ -f ~/.ssh/id_rsa ]; then
    rm -f ~/.ssh/id_rsa ~/.ssh/id_rsa.pub
    echo "   ✓ Removed old SSH keys (id_rsa)"
fi

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
if [ -d "\$INNATE_OS_PATH/.git" ]; then
    cd "\$INNATE_OS_PATH"
    
    # Switch to main branch
    git checkout main 2>/dev/null || git checkout -b main
    echo "   ✓ Switched to main branch"
    
    # Delete all other local branches
    git branch | grep -v '^\* main\$' | grep -v '^  main\$' | while read branch; do
        git branch -D "\$branch" 2>/dev/null && echo "   ✓ Deleted branch: \$branch"
    done
    
    # Update remote to release repo
    git remote set-url origin "git@github.com:\$RELEASE_REPO.git"
    echo "   ✓ Git remote set to git@github.com:\$RELEASE_REPO.git"
    
    # Prune remote tracking branches
    git remote prune origin 2>/dev/null || true
    git fetch --prune 2>/dev/null || true
    echo "   ✓ Pruned stale remote branches"
else
    echo "   ⚠ innate-os not found at \$INNATE_OS_PATH"
    echo "     Clone it first with: git clone git@github.com:\$RELEASE_REPO.git \$INNATE_OS_PATH"
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
