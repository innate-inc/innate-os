cmake_minimum_required(VERSION 3.8)
project(manipulation)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Require C++17 for std::filesystem and structured bindings
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(cv_bridge REQUIRED)
find_package(OpenCV REQUIRED)
find_package(brain_messages REQUIRED)
find_package(nlohmann_json REQUIRED)

# Find MPI for HDF5 OpenMPI support
find_package(MPI REQUIRED)

# Find HDF5 - use OpenMPI version
set(HDF5_PREFER_PARALLEL TRUE)
find_package(HDF5 REQUIRED COMPONENTS C)

# Include directories
include_directories(
  include
  ${HDF5_INCLUDE_DIRS}
  ${MPI_C_INCLUDE_DIRS}
)

# C++ Recorder Node executable
add_executable(recorder_node_cpp
  src/recorder_main.cpp
  src/recorder_node.cpp
  src/episode_data.cpp
  src/task_manager.cpp
)

target_link_libraries(recorder_node_cpp
  ${HDF5_LIBRARIES}
  ${MPI_C_LIBRARIES}
  nlohmann_json::nlohmann_json
)

ament_target_dependencies(recorder_node_cpp
  rclcpp
  sensor_msgs
  geometry_msgs
  nav_msgs
  std_msgs
  std_srvs
  cv_bridge
  OpenCV
  brain_messages
)

# Install C++ executable
install(TARGETS
  recorder_node_cpp
  DESTINATION lib/${PROJECT_NAME}
)

# Install include directory
install(
  DIRECTORY include/
  DESTINATION include
)

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

ament_package()

install(
  DIRECTORY manipulation
  DESTINATION lib/${PROJECT_NAME}
)

install(
  DIRECTORY config
  DESTINATION share/${PROJECT_NAME}
)

install(
  DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

# Install Python scripts
install(
  PROGRAMS
    manipulation/TestPublisher.py
    manipulation/visualizer.py
    manipulation/test.py
    manipulation/policy.py
    manipulation/trajectory.py
    manipulation/policyViT.py
    manipulation/behavior_server.py
  DESTINATION lib/${PROJECT_NAME}
)
