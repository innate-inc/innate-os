cmake_minimum_required(VERSION 3.8)
project(innate_webrtc_streamer)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)
find_package(std_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)

# Install Python modules
ament_python_install_package(${PROJECT_NAME}
  PACKAGE_DIR innate_webrtc_streamer
)

# Install Python executable
install(PROGRAMS
  innate_webrtc_streamer/webrtc_streamer.py
  DESTINATION lib/${PROJECT_NAME}
)

# Install the launch folder
install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

# Config folder removed - simple streamer doesn't need it

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()


