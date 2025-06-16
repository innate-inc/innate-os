cmake_minimum_required(VERSION 3.5)
project(maurice_head)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclpy REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  # comment the line when a copyright and license is added to all source files
  set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips cpplint (only works in a git repo)
  # comment the line when this package is in a git repo and when
  # a copyright and license is added to all source files
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

# -----------------------
# C++ nodes
# -----------------------
# No nodes to build yet

# -----------------------
# Python modules
# -----------------------
# this macro will install everything under the 'maurice_head' directory
ament_python_install_package(${PROJECT_NAME})

# Install Python executables
install(
  PROGRAMS
    maurice_head/servo_node.py
    maurice_head/test.py
  DESTINATION lib/${PROJECT_NAME}
)

# -----------------------
# Install launch & config
# -----------------------
install(
  DIRECTORY launch config
  DESTINATION share/${PROJECT_NAME}
)

# finalize
ament_package()
