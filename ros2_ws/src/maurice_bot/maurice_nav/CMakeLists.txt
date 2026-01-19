cmake_minimum_required(VERSION 3.5)
project(maurice_nav)

# Find the ament_cmake package and any dependencies you need
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)  # for Python nodes
find_package(rclcpp REQUIRED)
find_package(rclcpp_lifecycle REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(behaviortree_cpp_v3 REQUIRED)
find_package(tf2_ros REQUIRED)
find_package(lifecycle_msgs REQUIRED)
find_package(nav2_msgs REQUIRED)
find_package(brain_messages REQUIRED)

# Include directories
include_directories(include)

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

# Build lifecycle service utilities library
add_library(lifecycle_service_utils SHARED
  src/lifecycle_service_utils.cpp
)
ament_target_dependencies(lifecycle_service_utils
  rclcpp
  lifecycle_msgs
  nav2_msgs
)

# Build mode_manager executable (includes mode_manager + navigate_to_pose_router)
add_executable(mode_manager
  src/mode_manager_main.cpp
  src/mode_manager.cpp
  src/navigate_to_pose_router.cpp
)
ament_target_dependencies(mode_manager
  rclcpp
  rclcpp_action
  rclcpp_lifecycle
  tf2_ros
  geometry_msgs
  nav_msgs
  std_msgs
  nav2_msgs
  brain_messages
  lifecycle_msgs
)
target_link_libraries(mode_manager lifecycle_service_utils)

# Install C++ targets
install(TARGETS
  lifecycle_service_utils
  mode_manager
  DESTINATION lib/${PROJECT_NAME}
)

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

# ========== Testing ==========
if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)

  # Test for lifecycle service utilities
  ament_add_gtest(test_lifecycle_service_utils
    test/test_lifecycle_service_utils.cpp
  )
  target_link_libraries(test_lifecycle_service_utils
    lifecycle_service_utils
  )
  ament_target_dependencies(test_lifecycle_service_utils
    rclcpp
    lifecycle_msgs
  )

  # Test for mode manager
  ament_add_gtest(test_mode_manager
    test/test_mode_manager.cpp
    src/mode_manager.cpp
  )
  target_link_libraries(test_mode_manager
    lifecycle_service_utils
  )
  ament_target_dependencies(test_mode_manager
    rclcpp
    rclcpp_lifecycle
    tf2_ros
    geometry_msgs
    nav_msgs
    std_msgs
    nav2_msgs
    brain_messages
    lifecycle_msgs
  )
endif()

ament_package()
