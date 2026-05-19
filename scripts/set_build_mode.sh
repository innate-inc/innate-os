#!/bin/bash
# Script to easily switch between simulation and hardware build modes

set -e

MODE=$1

if [ -z "$MODE" ]; then
    echo "Usage: $0 <simulation|hardware>"
    echo ""
    echo "Current mode in docker-compose.dev.yml:"
    grep -A 2 "args:" docker-compose.dev.yml | grep "MODE:" || echo "  MODE: simulation (default)"
    exit 1
fi

if [ "$MODE" != "simulation" ] && [ "$MODE" != "hardware" ]; then
    echo "Error: MODE must be 'simulation' or 'hardware'"
    exit 1
fi

COMPOSE_FILE="docker-compose.dev.yml"

# Check if the file exists
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "Error: $COMPOSE_FILE not found"
    exit 1
fi

# Update the MODE in docker-compose.dev.yml
if grep -q "MODE:" "$COMPOSE_FILE"; then
    # MODE line exists, update it
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' "s/MODE: .*/MODE: $MODE  # Change to 'simulation' or 'hardware'/" "$COMPOSE_FILE"
    else
        # Linux
        sed -i "s/MODE: .*/MODE: $MODE  # Change to 'simulation' or 'hardware'/" "$COMPOSE_FILE"
    fi
    echo "✓ Updated $COMPOSE_FILE to MODE: $MODE"
else
    echo "Warning: Could not find MODE setting in $COMPOSE_FILE"
    echo "Please manually add it under the build args section:"
    echo ""
    echo "  innate:"
    echo "    build:"
    echo "      args:"
    echo "        MODE: $MODE"
    exit 1
fi

echo ""
echo "Next steps:"
echo "  1. Rebuild the Docker image:"
echo "     docker compose -f docker-compose.dev.yml build"
echo ""
echo "  2. Start the container:"
echo "     docker compose -f docker-compose.dev.yml up -d"
echo ""
echo "  3. Enter the container:"
echo "     docker compose -f docker-compose.dev.yml exec innate zsh -l"
