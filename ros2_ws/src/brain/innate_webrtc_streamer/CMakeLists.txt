cmake_minimum_required(VERSION 3.8)
project(innate_webrtc_streamer)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Find dependencies
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(OpenCV REQUIRED)

# Find GStreamer
find_package(PkgConfig REQUIRED)
pkg_check_modules(GSTREAMER REQUIRED gstreamer-1.0 gstreamer-webrtc-1.0 gstreamer-sdp-1.0)

# Find nlohmann_json
find_package(nlohmann_json QUIET)
if(NOT nlohmann_json_FOUND)
  pkg_check_modules(NLOHMANN_JSON nlohmann_json)
endif()

# C++ executable
add_executable(webrtc_streamer_node
  src/webrtc_streamer.cpp
)

target_include_directories(webrtc_streamer_node PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
  ${GSTREAMER_INCLUDE_DIRS}
  ${OpenCV_INCLUDE_DIRS}
)

target_link_libraries(webrtc_streamer_node
  ${GSTREAMER_LIBRARIES}
  ${OpenCV_LIBS}
)

if(nlohmann_json_FOUND)
  target_link_libraries(webrtc_streamer_node nlohmann_json::nlohmann_json)
elseif(NLOHMANN_JSON_FOUND)
  target_include_directories(webrtc_streamer_node PUBLIC ${NLOHMANN_JSON_INCLUDE_DIRS})
endif()

ament_target_dependencies(webrtc_streamer_node
  rclcpp
  std_msgs
  sensor_msgs
)

# Install C++ executable
install(TARGETS webrtc_streamer_node
  DESTINATION lib/${PROJECT_NAME}
)

# Install include directory
install(DIRECTORY include/
  DESTINATION include
)

# Install the launch folder
install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()


