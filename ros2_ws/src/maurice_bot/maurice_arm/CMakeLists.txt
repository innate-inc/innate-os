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
find_package(tf2_ros REQUIRED)
find_package(geometry_msgs REQUIRED)

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

# Add arm node (dynamixel + robot + arm with head)
add_executable(arm
  maurice_arm/dynamixel.cpp
  maurice_arm/robot.cpp
  maurice_arm/arm.cpp
)

ament_target_dependencies(arm
  rclcpp
  sensor_msgs
  std_msgs
  std_srvs
  dynamixel_sdk
  tf2_ros
  geometry_msgs
)

# Install C++ targets
install(TARGETS
  camera_node
  arm
  DESTINATION lib/${PROJECT_NAME}
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
  maurice_arm/tf_plotter.py
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