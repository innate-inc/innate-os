#!/bin/bash
# Generate unique SSH deploy keys for multiple customers/robots
# Generates keys, adds them to GitHub, and creates deployment bundles
#
# Usage: ./generate-deploy-keys.sh [options]

set -e

# Defaults
OUTPUT_DIR="./deploy-keys"
CUSTOMER_FILE=""
COUNT=""
REPO=""
RELEASE_REPO=""
PREFIX="robot"
INNATE_OS_PATH="/home/jetson1/innate-os"

print_help() {
    echo "Generate SSH deploy keys for multiple customers/robots"
    echo ""
    echo "Usage: $0 -n <count> -r <owner/repo> --release <owner/repo> [options]"
    echo ""
    echo "Required:"
    echo "  -n, --count <N>           Number of keys to generate"
    echo "  -r, --repo <owner/repo>   GitHub repo to add deploy keys to (release repo)"
    echo "  --release <owner/repo>    Release repo SSH URL for robots (e.g., innate-inc/maurice-release)"
    echo "  GITHUB_TOKEN env var      Personal access token with repo admin access"
    echo ""
    echo "Options:"
    echo "  -f, --file <file>         File with customer names (instead of --count)"
    echo "  -o, --output <dir>        Output directory (default: ./deploy-keys)"
    echo "  --prefix <prefix>         Prefix for auto-generated names (default: robot)"
    echo "  --innate-path <path>      Path to innate-os on robot (default: /home/jetson1/innate-os)"
    echo "  -h, --help                Show this help"
    echo ""
    echo "Example:"
    echo "  export GITHUB_TOKEN=ghp_xxxxx"
    echo "  $0 -n 40 -r innate-inc/innate-os-release --release innate-inc/innate-os-release"
    echo ""
    echo "Output structure:"
    echo "  deploy-keys/"
    echo "  ├── robot-001/                    # Per-robot folder"
    echo "  │   ├── innate_deploy_key         # Private key (copy to robot)"
    echo "  │   └── innate_deploy_key.pub     # Public key (added to GitHub)"
    echo "  ├── robot-002/"
    echo "  │   └── ..."
    echo "  ├── deploy-keys.csv               # Master tracking spreadsheet"
    echo "  └── install-key-on-robot.sh       # Script to install on robot"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--count)
            COUNT="$2"
            shift 2
            ;;
        -f|--file)
            CUSTOMER_FILE="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -r|--repo)
            REPO="$2"
            shift 2
            ;;
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --release)
            RELEASE_REPO="$2"
            shift 2
            ;;
        --innate-path)
            INNATE_OS_PATH="$2"
            shift 2
            ;;
        -h|--help)
            print_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate required args
if [ -z "$REPO" ]; then
    echo "Error: --repo is required"
    echo "Run with --help for usage"
    exit 1
fi

if [ -z "$GITHUB_TOKEN" ]; then
    echo "Error: GITHUB_TOKEN environment variable is required"
    echo ""
    echo "Create a token at: https://github.com/settings/tokens"
    echo "Required scope: admin:repo (to add deploy keys)"
    echo ""
    echo "Then run:"
    echo "  export GITHUB_TOKEN=ghp_xxxxx"
    echo "  $0 $@"
    exit 1
fi

if [ -z "$RELEASE_REPO" ]; then
    echo "Error: --release is required (the repo robots will pull from)"
    echo "Run with --help for usage"
    exit 1
fi

# Determine customer list
CUSTOMERS=()
if [ -n "$CUSTOMER_FILE" ]; then
    if [ ! -f "$CUSTOMER_FILE" ]; then
        echo "Error: Customer file not found: $CUSTOMER_FILE"
        exit 1
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] && CUSTOMERS+=("$line")
    done < "$CUSTOMER_FILE"
elif [ -n "$COUNT" ]; then
    for i in $(seq 1 "$COUNT"); do
        CUSTOMERS+=("$(printf '%s-%03d' "$PREFIX" "$i")")
    done
else
    echo "Error: Specify either --count or --file"
    echo "Run with --help for usage"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Deploy Key Generator"
echo "  Deploy keys added to: $REPO"
echo "  Robots will pull from: $RELEASE_REPO"
echo "  Keys: ${#CUSTOMERS[@]}"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Create output directory
mkdir -p "$OUTPUT_DIR"

# CSV with all info needed for deployment
CSV_FILE="$OUTPUT_DIR/deploy-keys.csv"
echo "robot_id,private_key_path,public_key_path,fingerprint,github_key_id,github_added,created_at" > "$CSV_FILE"

# Counters
CREATED=0
SKIPPED=0
FAILED=0

# Generate keys
for customer in "${CUSTOMERS[@]}"; do
    ROBOT_DIR="$OUTPUT_DIR/$customer"
    KEY_FILE="$ROBOT_DIR/innate_deploy_key"
    
    # Skip if already exists
    if [ -f "$KEY_FILE" ]; then
        echo "⏭  $customer (already exists)"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi
    
    # Create robot directory
    mkdir -p "$ROBOT_DIR"
    
    # Generate key
    ssh-keygen -t ed25519 -f "$KEY_FILE" -N "" -C "deploy-$customer@innate-os" -q
    chmod 600 "$KEY_FILE"
    chmod 644 "$KEY_FILE.pub"
    
    # Get fingerprint
    FINGERPRINT=$(ssh-keygen -lf "$KEY_FILE.pub" | awk '{print $2}')
    
    # Add to GitHub
    PUBLIC_KEY=$(cat "$KEY_FILE.pub")
    RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST \
        -H "Authorization: token $GITHUB_TOKEN" \
        -H "Accept: application/vnd.github.v3+json" \
        "https://api.github.com/repos/$REPO/keys" \
        -d "{\"title\":\"deploy-$customer\",\"key\":\"$PUBLIC_KEY\",\"read_only\":true}")
    
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | sed '$d')
    
    if [ "$HTTP_CODE" = "201" ]; then
        GITHUB_KEY_ID=$(echo "$BODY" | grep -o '"id": *[0-9]*' | head -1 | grep -o '[0-9]*')
        echo "✓  $customer (GitHub key ID: $GITHUB_KEY_ID)"
        GITHUB_ADDED="yes"
        CREATED=$((CREATED + 1))
    else
        ERROR=$(echo "$BODY" | grep -o '"message":"[^"]*"' | head -1 | sed 's/"message":"\([^"]*\)"/\1/' || echo "HTTP $HTTP_CODE")
        echo "✗  $customer (GitHub failed: $ERROR)"
        GITHUB_KEY_ID=""
        GITHUB_ADDED="no"
        FAILED=$((FAILED + 1))
    fi
    
    # Add to CSV
    CREATED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "$customer,$KEY_FILE,$KEY_FILE.pub,$FINGERPRINT,$GITHUB_KEY_ID,$GITHUB_ADDED,$CREATED_AT" >> "$CSV_FILE"
done

# Summary
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  COMPLETE"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Created: $CREATED"
echo "  Skipped: $SKIPPED (already existed)"
echo "  Failed:  $FAILED"
echo ""
echo "  Output directory: $OUTPUT_DIR"
echo "  Tracking CSV:     $CSV_FILE"
echo ""
echo "  Structure:"
echo "  $OUTPUT_DIR/"
for customer in "${CUSTOMERS[@]:0:3}"; do
    echo "  ├── $customer/"
    echo "  │   ├── innate_deploy_key      ← key file"
    echo "  │   └── innate_deploy_key.pub"
done
echo "  ├── ..."
echo "  └── deploy-keys.csv               ← master tracking file"
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "To deploy a key to a robot:"
echo "  ./scripts/ejection/install-key-on-robot.sh 1 jetson1@<robot-ip>"
echo ""
