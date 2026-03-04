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
find_package(tf2_msgs REQUIRED)
find_package(tf2_geometry_msgs REQUIRED)
find_package(visualization_msgs REQUIRED)
find_package(urdf REQUIRED)
find_package(tf2 REQUIRED)
find_package(nav2_core REQUIRED)
find_package(nav2_util REQUIRED)
find_package(nav2_costmap_2d REQUIRED)
find_package(pluginlib REQUIRED)
find_package(angles REQUIRED)

# Include directories
include_directories(include)

# Build shared library for DynamicGoalChecker plugin
add_library(dynamic_goal_checker SHARED
  src/plugins/dynamic_goal_checker.cpp
)
ament_target_dependencies(dynamic_goal_checker
  rclcpp
  rclcpp_lifecycle
  geometry_msgs
  nav2_core
  nav2_util
  nav2_costmap_2d
  pluginlib
  tf2
  tf2_ros
  angles
)
target_compile_definitions(dynamic_goal_checker PUBLIC "PLUGINLIB__DISABLE_BOOST_FUNCTIONS")

# Install the plugin library
install(TARGETS dynamic_goal_checker
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
)

# Install headers
install(
  DIRECTORY include/
  DESTINATION include
)

# Export plugin description file
pluginlib_export_plugin_description_file(nav2_core plugins.xml)

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

# Build C++ executable for dynamic_footprint node
add_executable(dynamic_footprint src/dynamic_footprint.cpp)
ament_target_dependencies(dynamic_footprint
  rclcpp
  geometry_msgs
  tf2_msgs
  tf2_ros
  tf2_geometry_msgs
  visualization_msgs
  urdf
)
install(TARGETS dynamic_footprint DESTINATION lib/${PROJECT_NAME})

# Build Switch BT control node plugin
# Build BT control node plugins
add_library(maurice_nav_bt_nodes SHARED
  src/switch_bt_node.cpp
)
target_include_directories(maurice_nav_bt_nodes PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)
ament_target_dependencies(maurice_nav_bt_nodes
  rclcpp
  std_msgs
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
  maurice_nav/odom_to_tf_node.py
  maurice_nav/goal_approach_scaler.py
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

# Export dependencies for downstream packages
ament_export_include_directories(include)
ament_export_libraries(dynamic_goal_checker)
ament_export_dependencies(
  rclcpp
  rclcpp_lifecycle
  geometry_msgs
  nav2_core
  nav2_util
  nav2_costmap_2d
  pluginlib
  tf2
  tf2_ros
  angles
)

ament_package()
