cmake_minimum_required(VERSION 3.5)
project(maurice_nav)

# Find the ament_cmake package and any dependencies you need
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)  # for Python nodes
find_package(rclcpp REQUIRED)
find_package(rclcpp_lifecycle REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(behaviortree_cpp_v3 REQUIRED)
find_package(tf2_ros REQUIRED)

# Build C++ executable for null_map_node
add_executable(null_map_node src/null_map_node.cpp)
ament_target_dependencies(null_map_node
  rclcpp
  rclcpp_lifecycle
  geometry_msgs
  nav_msgs
  tf2_ros
)
install(TARGETS null_map_node DESTINATION lib/${PROJECT_NAME})

# Build Switch BT control node plugin
add_library(maurice_nav_bt_nodes SHARED
  src/switch_bt_node.cpp
)
ament_target_dependencies(maurice_nav_bt_nodes
  behaviortree_cpp_v3
)
install(TARGETS maurice_nav_bt_nodes
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
)

# Install the launch files (so they can be found by ROS2)
install(
  DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

# Install configuration files
install(
  DIRECTORY config
  DESTINATION share/${PROJECT_NAME}
)

# Install Python scripts/programs
# Install Python modules and scripts
ament_python_install_package(${PROJECT_NAME})


install(PROGRAMS
  maurice_nav/mode_manager.py
  maurice_nav/grid_localizer.py
  DESTINATION lib/${PROJECT_NAME}
)

# Install package.xml
install(
  FILES package.xml
  DESTINATION share/${PROJECT_NAME}
)

# Install package.xml for package indexing
install(
  FILES package.xml
  DESTINATION share/ament_index/resource_index/packages
)

ament_package()
