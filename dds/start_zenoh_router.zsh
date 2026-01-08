#!/bin/zsh

# This script either starts a default Zenoh router when triggered without any arguments (by default from systemd).
# Or, it starts a Zenoh Router which connects to another Zenoh router, linking two machines running ROS.

# Source the user's zshrc and ROS environment, then start the Zenoh DDS server
. ~/.zshrc
if [ -f /opt/ros/humble/setup.sh ]; then
  . /opt/ros/humble/setup.sh
else
  echo "ROS environment setup file not found. Ensure ROS is installed."
  exit 1
fi
source "$(dirname "$0")/setup_dds.zsh"

export ZENOH_CONFIG_OVERRIDE="$ZENOH_ROUTER_CONFIG_OVERRIDE"

# Check if an argument is provided
if [ -n "$1" ]; then
  # echo "Initiating a satellite zenoh router for debugging"
  # Validate that the host:port is reachable
  echo "Checking connection to $1:7447..."
  if nc -z -w 5 "$1" 7447 2>/dev/null; then
    echo "Running Zenoh in satellite router mode"
    export ZENOH_CONFIG_OVERRIDE="connect/endpoints=[\"tcp/$1:7447\"]"
    # export RUST_LOG=debug # get logs out of Zenoh

    ros2 daemon stop > /dev/null # stop any stray dds or zenoh instances

    echo "Make sure to source innate-os/dds/setup_dds.zsh in every terminal to load the zenoh config into ROS"
  else
    echo "Error: Cannot connect to $1:7447"
    exit 1
  fi
else
  echo "Running Zenoh in default router mode"
fi

exec ros2 run rmw_zenoh_cpp rmw_zenohd
