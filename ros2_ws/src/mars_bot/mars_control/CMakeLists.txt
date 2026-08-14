cmake_minimum_required(VERSION 3.10)
project(mars_control)

# C++17 required for std::filesystem
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(rclpy REQUIRED)
find_package(rclcpp REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(mars_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(brain_messages REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(yaml-cpp REQUIRED)

# ============================================================================
# C++ Executable: app.cpp
# ============================================================================
add_executable(app.cpp mars_control/app.cpp mars_control/drive_smoother.cpp)

target_link_libraries(app.cpp
  nlohmann_json::nlohmann_json
  yaml-cpp
)

ament_target_dependencies(app.cpp
  rclcpp
  geometry_msgs
  nav_msgs
  brain_messages
  std_msgs
  std_srvs
  sensor_msgs
  mars_msgs
)

# ============================================================================
# C++ Executable: udp_leader_receiver.cpp
# ============================================================================
add_executable(udp_leader_receiver mars_control/udp_leader_receiver.cpp)

ament_target_dependencies(udp_leader_receiver
  rclcpp
  sensor_msgs
  std_srvs
)

install(TARGETS
  app.cpp
  udp_leader_receiver
  DESTINATION lib/${PROJECT_NAME}
)

# ============================================================================
# Python modules and executables
# ============================================================================
# Install Python modules
ament_python_install_package(${PROJECT_NAME})

# Install Python executables
install(PROGRAMS
  mars_control/keyboard.py
  mars_control/joystick.py
  mars_control/leader.py
  mars_control/motor_sound.py
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

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()

  find_package(ament_cmake_gtest REQUIRED)
  ament_add_gtest(test_hostname_utils test/test_hostname_utils.cpp)
endif()

ament_package()