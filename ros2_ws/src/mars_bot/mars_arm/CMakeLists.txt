cmake_minimum_required(VERSION 3.10)
project(mars_arm)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Find dependencies
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(cv_bridge REQUIRED)
find_package(OpenCV REQUIRED)
find_package(dynamixel_sdk REQUIRED)
find_package(Eigen3 REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(trajectory_msgs REQUIRED)
find_package(visualization_msgs REQUIRED)

# Mars messages
find_package(mars_msgs REQUIRED)

# Include directories
include_directories(include)

# Add arm node (dynamixel + robot + arm split into logical modules)
add_executable(arm
  mars_arm/dynamixel.cpp
  mars_arm/robot.cpp
  mars_arm/arm_node.cpp
  mars_arm/arm_config.cpp
  mars_arm/arm_control.cpp
  mars_arm/arm_services.cpp
  mars_arm/arm_trajectory.cpp
)

ament_target_dependencies(arm
  rclcpp
  sensor_msgs
  std_msgs
  std_srvs
  visualization_msgs
  dynamixel_sdk
  Eigen3
  mars_msgs
  geometry_msgs
  trajectory_msgs
)

target_include_directories(arm PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)

# Install C++ executables
install(TARGETS
  arm
  DESTINATION lib/${PROJECT_NAME}
)

# Install headers
install(DIRECTORY include/
  DESTINATION include
)

# Install Python files
install(FILES
  mars_arm/__init__.py
  DESTINATION lib/${PROJECT_NAME}
)

# Set Python version
set(PYTHON_VERSION "python3.10")

# Install Python module to site-packages
install(
  DIRECTORY mars_arm/
  DESTINATION lib/${PYTHON_VERSION}/site-packages/mars_arm
)

# Make Python files executable
install(PROGRAMS
  mars_arm/ik.py
  mars_arm/pid_hot_reload.py
  DESTINATION lib/${PROJECT_NAME}
)

# Install launch files
install(DIRECTORY
  launch/
  DESTINATION share/${PROJECT_NAME}/launch
)

# Install config files
install(DIRECTORY
  config/
  DESTINATION share/${PROJECT_NAME}/config
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  find_package(ament_cmake_gtest REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()

  ament_add_gtest(test_effort_conversion test/test_effort_conversion.cpp)
  target_include_directories(test_effort_conversion PRIVATE include)
endif()

ament_package()
