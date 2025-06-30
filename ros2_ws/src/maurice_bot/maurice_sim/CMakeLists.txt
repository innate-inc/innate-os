cmake_minimum_required(VERSION 3.8)
project(maurice_sim)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Find dependencies
find_package(ament_cmake REQUIRED)
find_package(rclpy REQUIRED)

# Install Python module
install(
  DIRECTORY maurice_sim
  DESTINATION lib/${PROJECT_NAME}
)

# Install launch files
install(
  DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

# Install config files
install(
  DIRECTORY config
  DESTINATION share/${PROJECT_NAME}
)

# Install URDF files
install(
  DIRECTORY urdf
  DESTINATION share/${PROJECT_NAME}
)

# Install mesh files
install(
  DIRECTORY meshes
  DESTINATION share/${PROJECT_NAME}
)

# Install worlds files
install(
  DIRECTORY worlds
  DESTINATION share/${PROJECT_NAME}
)

# Install mjcf files
install(
  DIRECTORY mjcf
  DESTINATION share/${PROJECT_NAME}
)

# Install programs
install(
  PROGRAMS
    maurice_sim/sim.py
  DESTINATION lib/${PROJECT_NAME}
)

# Tests
if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
