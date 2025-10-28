#!/bin/zsh
alias discovery="$INNATE_OS_ROOT/dds/discovery.zsh"
alias discovery-and-launch-sim="$INNATE_OS_ROOT/dds/discovery-and-launch-sim.zsh"


export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID=0

# Dynamically determine the primary IP address
# Uses 'hostname -I' which lists all IPs, takes the first one. Adjust if needed.
# Fallback to localhost if no IP found (e.g., no network connection yet)
CURRENT_IP=$(hostname -I | awk '{print $1}')
if [[ -z "$CURRENT_IP" ]]; then
  echo "WARNING: Could not determine primary IP address. Falling back to 127.0.0.1 for ROS_DISCOVERY_SERVER_IP." >&2
  CURRENT_IP="127.0.0.1"
fi
export ROS_DISCOVERY_SERVER_IP=${CURRENT_IP}

# Original override mechanism removed. IP is now always dynamic based on current state.
# export ROS_DISCOVERY_SERVER_IP=${ROS_DISCOVERY_SERVER_IP_OVERRIDE:-10.92.199.46}
export ROS_DISCOVERY_SERVER_PORT=11811
export ROS_DISCOVERY_SERVER=$ROS_DISCOVERY_SERVER_IP:$ROS_DISCOVERY_SERVER_PORT


# Generate DDS configuration file from template
# Ensure the directory exists
DDS_CONFIG_DIR=$(dirname "$INNATE_OS_ROOT/dds/super_client_configuration.xml")
mkdir -p "$DDS_CONFIG_DIR"

export FASTRTPS_DEFAULT_PROFILES_FILE=$INNATE_OS_ROOT/dds/super_client_configuration.xml
sed -e "s/DDS_SERVER_IP/$ROS_DISCOVERY_SERVER_IP/g" \
    -e "s/DDS_SERVER_PORT/$ROS_DISCOVERY_SERVER_PORT/g" \
    $INNATE_OS_ROOT/dds/super_client_template.xml > $FASTRTPS_DEFAULT_PROFILES_FILE

# Add a log message indicating the IP being used
echo "ROS DDS configured with Discovery Server IP: $ROS_DISCOVERY_SERVER_IP" >&2
