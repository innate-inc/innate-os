#!/bin/zsh
alias discovery="/home/vignesh/maurice-prod/dds/discovery.zsh"


export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID=0


export ROS_DISCOVERY_SERVER_IP=192.168.1.124
export ROS_DISCOVERY_SERVER_PORT=11811
export ROS_DISCOVERY_SERVER=$ROS_DISCOVERY_SERVER_IP:$ROS_DISCOVERY_SERVER_PORT
# Create directory if it doesn't exist
mkdir -p $HOME/maurice-prod/dds

# Generate DDS configuration file from template
export FASTRTPS_DEFAULT_PROFILES_FILE=$HOME/maurice-prod/dds/super_client_configuration.xml
sed -e "s/DDS_SERVER_IP/$ROS_DISCOVERY_SERVER_IP/g" \
    -e "s/DDS_SERVER_PORT/$ROS_DISCOVERY_SERVER_PORT/g" \
    $HOME/maurice-prod/dds/super_client_template.xml > $FASTRTPS_DEFAULT_PROFILES_FILE