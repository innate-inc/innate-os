#!/bin/zsh
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
export ROS_DOMAIN_ID=0
export ZENOH_ROUTER_CHECK_ATTEMPTS=0 # wait forever for router start

# enabled shared memory transport w/ 12 MiB buffer per node
# TODO: once we move nodes to be individually launchable, this value 
# should be set on a per-node basis to avoid wasting 12MiB per node 
# (or potentially bottlenecking the camera nodes)
export ZENOH_CONFIG_OVERRIDE='transport/shared_memory/enabled=true;transport/shared_memory/transport_optimization/pool_size=12582912'
