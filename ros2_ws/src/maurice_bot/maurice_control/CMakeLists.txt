cmake_minimum_required(VERSION 3.8)
project(maurice_control)

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
find_package(maurice_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(yaml-cpp REQUIRED)

# ============================================================================
# C++ Executable: app.cpp
# ============================================================================
add_executable(app.cpp maurice_control/app.cpp)

target_link_libraries(app.cpp
  nlohmann_json::nlohmann_json
  yaml-cpp
)

ament_target_dependencies(app.cpp
  rclcpp
  geometry_msgs
  std_msgs
  maurice_msgs
)

# ============================================================================
# C++ Executable: udp_leader_receiver.cpp
# ============================================================================
find_package(std_srvs REQUIRED)

add_executable(udp_leader_receiver maurice_control/udp_leader_receiver.cpp)

ament_target_dependencies(udp_leader_receiver
  rclcpp
  std_msgs
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
  maurice_control/keyboard.py
  maurice_control/joystick.py
  maurice_control/leader.py
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
endif()

ament_package()