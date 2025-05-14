cmake_minimum_required(VERSION 3.8)
project(maurice_bringup)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclpy REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(action_msgs REQUIRED)
find_package(tf2 REQUIRED)
find_package(tf2_ros REQUIRED)
find_package(tf2_geometry_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(depthai_examples REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(camera_info_manager REQUIRED)
find_package(depthai REQUIRED)
find_package(depthai_bridge REQUIRED)
find_package(OpenCV REQUIRED)

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

# Install Python modules
ament_python_install_package(${PROJECT_NAME}
  PACKAGE_DIR maurice_bringup
)

# Install Python executables
install(PROGRAMS
  maurice_bringup/bringup.py
  DESTINATION lib/${PROJECT_NAME}
)

# Install launch files
install(DIRECTORY
  launch
  DESTINATION share/${PROJECT_NAME}
)

# Install config files
install(DIRECTORY
  config
  DESTINATION share/${PROJECT_NAME}
)

# --- Build and install camera_driver C++ node ---
# Assuming camera.cpp is directly in a subdirectory named 'maurice_bringup' 
# under your package root (e.g., src/maurice_bot/maurice_bringup/maurice_bringup/camera.cpp).
# If camera.cpp is in src/ (e.g. src/maurice_bot/maurice_bringup/src/camera.cpp), then use src/camera.cpp
add_executable(camera_driver maurice_bringup/camera.cpp) 
# Or if in src/: add_executable(camera_driver src/camera.cpp)

ament_target_dependencies(camera_driver
    rclcpp                # Core ROS 2 C++ client library
    sensor_msgs           # For sensor_msgs::msg::Image and CameraInfo
    camera_info_manager   # For CameraInfoManager
    depthai               # Core DepthAI library
    depthai_bridge        # For DepthAI to ROS message conversion utilities
)

target_link_libraries(camera_driver ${OpenCV_LIBS})

install(TARGETS
  camera_driver
  DESTINATION lib/${PROJECT_NAME}
)

ament_package()
