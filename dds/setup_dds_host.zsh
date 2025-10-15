#!/bin/zsh
alias discovery="$INNATE_OS_ROOT/dds/discovery.zsh"
alias discovery-and-launch-sim="$INNATE_OS_ROOT/dds/discovery-and-launch-sim.zsh"


export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID=0

# Set the discovery server IP
export ROS_DISCOVERY_SERVER_IP="192.168.1.120"

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
