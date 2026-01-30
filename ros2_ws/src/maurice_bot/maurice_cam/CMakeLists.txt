cmake_minimum_required(VERSION 3.8)
project(maurice_cam)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Find dependencies
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_components REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(image_transport REQUIRED)
find_package(tf2_ros REQUIRED)
find_package(geometry_msgs REQUIRED)

# Find OpenCV
find_package(OpenCV REQUIRED)

# Find GStreamer (for webrtc_streamer)
find_package(PkgConfig REQUIRED)
pkg_check_modules(TURBOJPEG REQUIRED libturbojpeg)
pkg_check_modules(GSTREAMER REQUIRED gstreamer-1.0 gstreamer-webrtc-1.0 gstreamer-sdp-1.0)

# Find nlohmann_json (for webrtc_streamer and stereo_depth_estimator)
find_package(nlohmann_json QUIET)
if(NOT nlohmann_json_FOUND)
  pkg_check_modules(NLOHMANN_JSON nlohmann_json)
endif()

# Find VPI (NVIDIA Vision Programming Interface) for stereo depth estimation
# VPI is only required on NVIDIA Jetson hardware, not for simulation
set(VPI_ROOT "/opt/nvidia/vpi3")
find_path(VPI_INCLUDE_DIR vpi/VPI.h PATHS ${VPI_ROOT}/include)
find_library(VPI_LIBRARY nvvpi PATHS ${VPI_ROOT}/lib64 ${VPI_ROOT}/lib)

if(VPI_INCLUDE_DIR AND VPI_LIBRARY)
  set(VPI_FOUND TRUE)
  message(STATUS "Found VPI: ${VPI_LIBRARY}")
  message(STATUS "VPI-dependent components (stereo_depth_estimator) will be built")
else()
  set(VPI_FOUND FALSE)
  message(WARNING "VPI not found at ${VPI_ROOT}. Stereo depth estimator will not be built.")
  message(WARNING "This is expected for simulation builds. For hardware builds, install nvidia-vpi-dev package.")
endif()

# Set Python version
set(PYTHON_VERSION "python3.10")

# Include directories
include_directories(include)

# =============================================================================
# COMPONENT LIBRARIES (for composition / intra-process communication)
# =============================================================================

# Main camera driver component library
add_library(main_camera_driver_component SHARED maurice_cam/main_camera_driver.cpp)
target_compile_definitions(main_camera_driver_component PRIVATE BUILDING_COMPONENT_LIBRARY)
target_include_directories(main_camera_driver_component PRIVATE ${TURBOJPEG_INCLUDE_DIRS})
target_link_libraries(main_camera_driver_component ${OpenCV_LIBS} ${TURBOJPEG_LIBRARIES})
ament_target_dependencies(main_camera_driver_component 
  rclcpp 
  rclcpp_components
  sensor_msgs
  image_transport
)
rclcpp_components_register_nodes(main_camera_driver_component "maurice_cam::MainCameraDriver")

# Arm camera driver component library
add_library(arm_camera_driver_component SHARED maurice_cam/arm_camera_driver.cpp)
target_compile_definitions(arm_camera_driver_component PRIVATE BUILDING_COMPONENT_LIBRARY)
target_link_libraries(arm_camera_driver_component ${OpenCV_LIBS})
ament_target_dependencies(arm_camera_driver_component 
  rclcpp 
  rclcpp_components
  sensor_msgs
)
rclcpp_components_register_nodes(arm_camera_driver_component "maurice_cam::ArmCameraDriver")

# WebRTC streamer component library
add_library(webrtc_streamer_component SHARED maurice_cam/webrtc_streamer.cpp)
target_compile_definitions(webrtc_streamer_component PRIVATE BUILDING_COMPONENT_LIBRARY)
target_include_directories(webrtc_streamer_component PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
  ${GSTREAMER_INCLUDE_DIRS}
  ${OpenCV_INCLUDE_DIRS}
)
target_link_libraries(webrtc_streamer_component
  ${GSTREAMER_LIBRARIES}
  ${OpenCV_LIBS}
)
if(nlohmann_json_FOUND)
  target_link_libraries(webrtc_streamer_component nlohmann_json::nlohmann_json)
elseif(NLOHMANN_JSON_FOUND)
  target_include_directories(webrtc_streamer_component PUBLIC ${NLOHMANN_JSON_INCLUDE_DIRS})
endif()
ament_target_dependencies(webrtc_streamer_component
  rclcpp
  rclcpp_components
  std_msgs
  sensor_msgs
)
rclcpp_components_register_nodes(webrtc_streamer_component "maurice_cam::WebRTCStreamer")

# Stereo depth estimator component library (requires VPI)
if(VPI_FOUND)
  add_library(stereo_depth_estimator_component SHARED maurice_cam/stereo_depth_estimator.cpp)
  target_compile_definitions(stereo_depth_estimator_component PRIVATE BUILDING_COMPONENT_LIBRARY)
  target_include_directories(stereo_depth_estimator_component PRIVATE ${VPI_INCLUDE_DIR})
  target_link_libraries(stereo_depth_estimator_component ${OpenCV_LIBS} ${VPI_LIBRARY})
  if(nlohmann_json_FOUND)
    target_link_libraries(stereo_depth_estimator_component nlohmann_json::nlohmann_json)
  elseif(NLOHMANN_JSON_FOUND)
    target_include_directories(stereo_depth_estimator_component PRIVATE ${NLOHMANN_JSON_INCLUDE_DIRS})
  endif()
  ament_target_dependencies(stereo_depth_estimator_component 
    rclcpp 
    rclcpp_components
    sensor_msgs
  )
  rclcpp_components_register_nodes(stereo_depth_estimator_component "maurice_cam::StereoDepthEstimator")
endif()

# =============================================================================
# STANDALONE EXECUTABLES (for running nodes individually)
# =============================================================================

# Create main camera driver executable
add_executable(main_camera_driver maurice_cam/main_camera_driver.cpp)
target_include_directories(main_camera_driver PRIVATE ${TURBOJPEG_INCLUDE_DIRS})
target_link_libraries(main_camera_driver ${OpenCV_LIBS} ${TURBOJPEG_LIBRARIES})
ament_target_dependencies(main_camera_driver 
  rclcpp 
  rclcpp_components
  sensor_msgs
  image_transport
)

# Create arm camera driver executable
add_executable(arm_camera_driver maurice_cam/arm_camera_driver.cpp)
target_link_libraries(arm_camera_driver ${OpenCV_LIBS})
ament_target_dependencies(arm_camera_driver 
  rclcpp 
  rclcpp_components
  sensor_msgs
)

# Create WebRTC streamer executable
add_executable(webrtc_streamer_node maurice_cam/webrtc_streamer.cpp)
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
  rclcpp_components
  std_msgs
  sensor_msgs
)

# Create stereo depth estimator executable (requires VPI)
if(VPI_FOUND)
  add_executable(stereo_depth_estimator maurice_cam/stereo_depth_estimator.cpp)
  target_include_directories(stereo_depth_estimator PRIVATE ${VPI_INCLUDE_DIR})
  target_link_libraries(stereo_depth_estimator ${OpenCV_LIBS} ${VPI_LIBRARY})
  if(nlohmann_json_FOUND)
    target_link_libraries(stereo_depth_estimator nlohmann_json::nlohmann_json)
  elseif(NLOHMANN_JSON_FOUND)
    target_include_directories(stereo_depth_estimator PRIVATE ${NLOHMANN_JSON_INCLUDE_DIRS})
  endif()
  ament_target_dependencies(stereo_depth_estimator
    rclcpp
    rclcpp_components
    sensor_msgs
  )
endif()

# =============================================================================
# INSTALLATION
# =============================================================================

# Install component libraries
set(COMPONENT_LIBRARIES
  main_camera_driver_component
  arm_camera_driver_component
  webrtc_streamer_component
)
if(VPI_FOUND)
  list(APPEND COMPONENT_LIBRARIES stereo_depth_estimator_component)
endif()

install(TARGETS ${COMPONENT_LIBRARIES}
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
)

# Install C++ executables
set(EXECUTABLE_TARGETS
  main_camera_driver
  arm_camera_driver
  webrtc_streamer_node
)
if(VPI_FOUND)
  list(APPEND EXECUTABLE_TARGETS stereo_depth_estimator)
endif()

install(TARGETS ${EXECUTABLE_TARGETS}
  DESTINATION lib/${PROJECT_NAME}
)

# Install Python module to site-packages (makes it importable as a Python package)
install(
  DIRECTORY maurice_cam/
  DESTINATION lib/${PYTHON_VERSION}/site-packages/maurice_cam
  PATTERN "*.cpp" EXCLUDE
)

# Install Python executables
install(PROGRAMS
  maurice_cam/stereo_calibrator.py
  DESTINATION lib/${PROJECT_NAME}
  RENAME stereo_calibrator
)

# Install launch files
install(DIRECTORY launch/
  DESTINATION share/${PROJECT_NAME}/launch
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
