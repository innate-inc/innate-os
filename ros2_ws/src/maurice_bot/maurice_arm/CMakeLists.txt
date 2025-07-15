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
find_package(cv_bridge REQUIRED)
find_package(OpenCV REQUIRED)

# Add C++ executable
add_executable(camera_node maurice_arm/camera.cpp)

# Link C++ dependencies
ament_target_dependencies(camera_node
  rclcpp
  sensor_msgs
  cv_bridge
)

target_link_libraries(camera_node
  ${OpenCV_LIBS}
)

# Install C++ targets
install(TARGETS
  camera_node
  DESTINATION lib/${PROJECT_NAME}
)

# Install Python files
install(FILES
  maurice_arm/__init__.py
  maurice_arm/dynamixel.py
  maurice_arm/robot.py
  maurice_arm/arm.py
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
  maurice_arm/arm.py
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