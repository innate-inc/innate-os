#!/bin/zsh
# This script initializes the environment variables for every ROS node's communication

export RMW_IMPLEMENTATION=rmw_zenoh_cpp
export ROS_DOMAIN_ID=0
export ZENOH_ROUTER_CHECK_ATTEMPTS=0 # wait forever for router start

# enabled shared memory transport w/ 12 MiB buffer per node
# TODO: once we move nodes to be individually launchable, this value 
# should be set on a per-node basis to avoid wasting 12MiB per node 
# (or potentially bottlenecking the camera nodes)
export ZENOH_SESSION_CONFIG_OVERRIDE='transport/shared_memory/enabled=true;transport/shared_memory/transport_optimization/pool_size=12582912;transport/link/tx/queue/congestion_control/drop/wait_before_drop=200000;transport/link/tx/queue/congestion_control/drop/max_wait_before_drop_fragments=300000'
export ZENOH_ROUTER_CONFIG_OVERRIDE='transport/shared_memory/enabled=false;transport/shared_memory/transport_optimization/pool_size=12582912;transport/link/tx/queue/congestion_control/drop/wait_before_drop=200000;transport/link/tx/queue/congestion_control/drop/max_wait_before_drop_fragments=300000'
export ZENOH_CONFIG_OVERRIDE="$ZENOH_SESSION_CONFIG_OVERRIDE"
# export RUST_LOG=debug
