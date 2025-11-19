cmake_minimum_required(VERSION 3.8)
project(maurice_arm)

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
find_package(fcl REQUIRED)
find_package(Eigen3 REQUIRED)
find_package(visualization_msgs REQUIRED)

# MoveIt dependencies
find_package(moveit_ros_planning_interface REQUIRED)
find_package(moveit_ros_planning REQUIRED)
find_package(moveit_core REQUIRED)

# Maurice messages
find_package(maurice_msgs REQUIRED)

# Include directories
include_directories(include)

# Add C++ executables
add_executable(camera_node maurice_arm/camera.cpp)

# Link camera_node dependencies
ament_target_dependencies(camera_node
  rclcpp
  sensor_msgs
  cv_bridge
)

target_link_libraries(camera_node
  ${OpenCV_LIBS}
)

# Create planner library
add_library(maurice_planner SHARED
  maurice_arm/planner.cpp
)

target_include_directories(maurice_planner PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)

ament_target_dependencies(maurice_planner
  rclcpp
  moveit_ros_planning_interface
  moveit_ros_planning
  moveit_core
)

# Add arm node (dynamixel + robot + arm with head + collision checker)
add_executable(arm
  maurice_arm/dynamixel.cpp
  maurice_arm/robot.cpp
  maurice_arm/collision_checker.cpp
  maurice_arm/arm.cpp
)

ament_target_dependencies(arm
  rclcpp
  sensor_msgs
  std_msgs
  std_srvs
  visualization_msgs
  dynamixel_sdk
  Eigen3
  maurice_msgs
)

target_link_libraries(arm
  fcl
  maurice_planner
)

target_include_directories(arm PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)

# Add collision checker node (standalone)
add_executable(collision_checker
  maurice_arm/collision_checker.cpp
  maurice_arm/collision_checker_main.cpp
)

ament_target_dependencies(collision_checker
  rclcpp
  sensor_msgs
  std_msgs
  visualization_msgs
  Eigen3
)

target_link_libraries(collision_checker
  fcl
)

target_include_directories(collision_checker PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)

# Install C++ executables
install(TARGETS
  camera_node
  arm
  collision_checker
  DESTINATION lib/${PROJECT_NAME}
)

# Install shared library (needs to go to lib/ not lib/${PROJECT_NAME}/)
install(TARGETS maurice_planner
  EXPORT maurice_planner
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
)

# Install headers
install(DIRECTORY include/
  DESTINATION include
)

# Export the library for downstream packages
ament_export_targets(maurice_planner HAS_LIBRARY_TARGET)
ament_export_dependencies(
  rclcpp
  moveit_ros_planning_interface
  moveit_ros_planning
  moveit_core
)

# Install Python files
install(FILES
  maurice_arm/__init__.py
  maurice_arm/arm_utils.py
  DESTINATION lib/${PROJECT_NAME}
)

# Set Python version
set(PYTHON_VERSION "python3.10")

# Install Python module to site-packages
install(
  DIRECTORY maurice_arm/
  DESTINATION lib/${PYTHON_VERSION}/site-packages/maurice_arm
)

# Make Python files executable
install(PROGRAMS
  maurice_arm/arm_utils.py
  maurice_arm/ik.py
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
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()