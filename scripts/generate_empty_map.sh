#!/usr/bin/env bash

# Generate an empty ROS occupancy grid map
# Width: 100 cells
# Height: 100 cells
# Resolution: 1.0 m/cell
# Range: -50m to +50m in both x and y
# All cells set to 0 (free space)

# Generate array of 10001 zeros (100x100)
data_array=$(printf '0,%.0s' {1..10000})
data_array="[${data_array%,}]"  # Remove trailing comma and wrap in brackets

# Publish the map
ros2 topic pub --once /map nav_msgs/msg/OccupancyGrid "{
  header: {
    stamp: {sec: 0, nanosec: 0},
    frame_id: 'map'
  },
  info: {
    map_load_time: {sec: 0, nanosec: 0},
    resolution: .05,
    width: 0,
    height: 0,
    origin: {
      position: {x: 0.0, y: 0.0, z: 0.0},
      orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}
    }
  },
  data: ${data_array}
}" &

ros2 run nav2_map_server map_saver_cli -f maps/empty_0size --ros-args -p save_map_timeout:=5000.0

wait

echo "Empty map published to /map topic and then saved"
