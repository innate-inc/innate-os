cmake_minimum_required(VERSION 3.8)
project(maurice_cam)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Find dependencies
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)

# Set Python version
set(PYTHON_VERSION "python3.10")

# Install Python module to site-packages (makes it importable as a Python package)
install(
  DIRECTORY maurice_cam/
  DESTINATION lib/${PYTHON_VERSION}/site-packages/maurice_cam
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()