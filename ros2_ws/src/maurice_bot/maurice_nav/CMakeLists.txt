cmake_minimum_required(VERSION 3.5)
project(maurice_nav)

# Find the ament_cmake package and any dependencies you need
find_package(ament_cmake REQUIRED)
find_package(rclpy REQUIRED)  # for Python nodes
# If you have any C++ nodes, you can also find rclcpp here

# Optionally, build any C++ executables here if needed
# For example:
# add_executable(my_cpp_node src/my_cpp_node.cpp)
# ament_target_dependencies(my_cpp_node rclcpp std_msgs)
# install(TARGETS my_cpp_node DESTINATION lib/${PROJECT_NAME})

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

# Install the Python modules. This will copy the 'maurice_nav' directory into the installation space.
install(
  DIRECTORY maurice_nav
  DESTINATION lib/${PROJECT_NAME}
)

# Install Python scripts/programs
install(PROGRAMS
  maurice_nav/__init__.py
  # Add more scripts as needed
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
