#!/bin/zsh
alias discovery="~/maurice-prod/dds/discovery.zsh"
alias discovery-and-launch-sim="~/maurice-prod/dds/discovery-and-launch-sim.zsh"


export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID=0


export ROS_DISCOVERY_SERVER_IP=${ROS_DISCOVERY_SERVER_IP_OVERRIDE:-192.168.1.124}
export ROS_DISCOVERY_SERVER_PORT=11811
export ROS_DISCOVERY_SERVER=$ROS_DISCOVERY_SERVER_IP:$ROS_DISCOVERY_SERVER_PORT


# Generate DDS configuration file from template
export FASTRTPS_DEFAULT_PROFILES_FILE=$HOME/maurice-prod/dds/super_client_configuration.xml
sed -e "s/DDS_SERVER_IP/$ROS_DISCOVERY_SERVER_IP/g" \
    -e "s/DDS_SERVER_PORT/$ROS_DISCOVERY_SERVER_PORT/g" \
    $HOME/maurice-prod/dds/super_client_template.xml > $FASTRTPS_DEFAULT_PROFILES_FILE
