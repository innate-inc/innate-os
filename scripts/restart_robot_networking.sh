#!/bin/bash
# This script is intended to be run via sudo by the BLE provisioning service
# when a network change requires restarting ROS/DDS components.

echo "Restarting Zenoh Server due to network change..." >&2
systemctl restart zenoh-router.service
if [ $? -ne 0 ]; then
  echo "ERROR: Failed to restart zenoh-router.service" >&2
  # Optionally exit here if discovery server restart is critical
  # exit 1
fi

echo "Restarting ROS Application due to network change..." >&2
systemctl restart ros-app.service
if [ $? -ne 0 ]; then
  echo "ERROR: Failed to restart ros-app.service" >&2
  # Optionally exit here if ROS app restart is critical
  # exit 1
fi

echo "Service restarts requested." >&2
exit 0 