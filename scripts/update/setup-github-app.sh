#!/bin/bash
# Setup GitHub App authentication for git
# Usage: ./setup-github-app.sh <APP_ID> <INSTALLATION_ID> <path-to-pem-file>

set -e

if [ $# -ne 3 ]; then
    echo "Usage: $0 <APP_ID> <INSTALLATION_ID> <path-to-pem-file>"
    echo ""
    echo "Example: $0 12345 67890 ~/Downloads/innate-os.pem"
    exit 1
fi

APP_ID="$1"
INSTALLATION_ID="$2"
PEM_FILE="$3"

echo "Setting up GitHub App authentication..."

# Copy PEM file
mkdir -p ~/.ssh
cp "$PEM_FILE" ~/.ssh/github-app.pem
chmod 600 ~/.ssh/github-app.pem
echo "✓ Installed private key to ~/.ssh/github-app.pem"

# Add to .zshrc
cat >> ~/.zshrc << EOF

# GitHub App for innate-os updates
export GITHUB_APP_ID="$APP_ID"
export GITHUB_INSTALLATION_ID="$INSTALLATION_ID"
export GITHUB_APP_PRIVATE_KEY="$HOME/.ssh/github-app.pem"
EOF

# Source for current session
export GITHUB_APP_ID="$APP_ID"
export GITHUB_INSTALLATION_ID="$INSTALLATION_ID"
export GITHUB_APP_PRIVATE_KEY="$HOME/.ssh/github-app.pem"

echo "✓ Added environment variables to ~/.zshrc"

# Configure git to use HTTPS
cd /home/jetson1/innate-os
git remote set-url origin https://github.com/innate-inc/maurice-prod.git
echo "✓ Set git remote to HTTPS"

# Configure git credential helper
git config credential.helper "/home/jetson1/innate-os/scripts/update/git-credential-github-app"
echo "✓ Configured git credential helper"

# Test it
echo ""
echo "Testing authentication..."
if /home/jetson1/innate-os/scripts/update/github-app-auth.sh > /dev/null; then
    echo "✓ Authentication successful!"
    echo ""
    echo "Try: cd /home/jetson1/innate-os && git pull"
else
    echo "✗ Authentication failed. Check your App ID and Installation ID."
    exit 1
fi


