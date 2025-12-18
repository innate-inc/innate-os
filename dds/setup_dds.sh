#!/bin/bash
# DDS Setup Script (Bash compatible version)
# This script configures FastDDS discovery server settings
# Source this before running ROS2 nodes

# Aliases (bash version)
alias discovery="$INNATE_OS_ROOT/dds/discovery.zsh"
alias discovery-and-launch-sim="$INNATE_OS_ROOT/dds/discovery-and-launch-sim.zsh"

export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID=0

# Dynamically determine the primary IP address
# Uses 'hostname -I' which lists all IPs, takes the first one. Adjust if needed.
# Fallback to localhost if no IP found (e.g., no network connection yet)
CURRENT_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$CURRENT_IP" ]; then
    echo "WARNING: Could not determine primary IP address. Falling back to 127.0.0.1 for ROS_DISCOVERY_SERVER_IP." >&2
    CURRENT_IP="127.0.0.1"
fi
export ROS_DISCOVERY_SERVER_IP=${CURRENT_IP}

export ROS_DISCOVERY_SERVER_PORT=11811
export ROS_DISCOVERY_SERVER=$ROS_DISCOVERY_SERVER_IP:$ROS_DISCOVERY_SERVER_PORT

# Generate DDS configuration file from template
if [ -z "$INNATE_OS_ROOT" ]; then
    echo "ERROR: INNATE_OS_ROOT is not set. Cannot configure DDS." >&2
    return 1 2>/dev/null || exit 1
fi

DDS_CONFIG_DIR="$INNATE_OS_ROOT/dds"
TEMPLATE_FILE="$DDS_CONFIG_DIR/super_client_template.xml"
export FASTRTPS_DEFAULT_PROFILES_FILE="$DDS_CONFIG_DIR/super_client_configuration.xml"

mkdir -p "$DDS_CONFIG_DIR"

if [ ! -f "$TEMPLATE_FILE" ]; then
    echo "ERROR: DDS template not found at $TEMPLATE_FILE" >&2
    return 1 2>/dev/null || exit 1
fi

# Generate the configuration file from template
sed -e "s/DDS_SERVER_IP/$ROS_DISCOVERY_SERVER_IP/g" \
    -e "s/DDS_SERVER_PORT/$ROS_DISCOVERY_SERVER_PORT/g" \
    "$TEMPLATE_FILE" > "$FASTRTPS_DEFAULT_PROFILES_FILE"

if [ ! -f "$FASTRTPS_DEFAULT_PROFILES_FILE" ]; then
    echo "ERROR: Failed to generate DDS config at $FASTRTPS_DEFAULT_PROFILES_FILE" >&2
    return 1 2>/dev/null || exit 1
fi

# Verify the generated config has the correct IP (not placeholders)
if grep -q "DDS_SERVER_IP" "$FASTRTPS_DEFAULT_PROFILES_FILE"; then
    echo "ERROR: DDS config still contains placeholder values!" >&2
    return 1 2>/dev/null || exit 1
fi

echo "DDS configured: Discovery Server at $ROS_DISCOVERY_SERVER" >&2
echo "  Config file: $FASTRTPS_DEFAULT_PROFILES_FILE" >&2
