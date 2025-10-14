cmake_minimum_required(VERSION 3.8)
project(maurice_cam)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# Find dependencies
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(cv_bridge REQUIRED)
find_package(image_transport REQUIRED)

# Find OpenCV
find_package(OpenCV REQUIRED)

# Set Python version
set(PYTHON_VERSION "python3.10")

# Include directories
include_directories(include)

# Create C++ camera driver executable
add_executable(camera_driver maurice_cam/camera_driver.cpp)
ament_target_dependencies(camera_driver 
  rclcpp 
  sensor_msgs
  cv_bridge
  image_transport
)
target_link_libraries(camera_driver ${OpenCV_LIBS})

# Install C++ executable
install(TARGETS
  camera_driver
  DESTINATION lib/${PROJECT_NAME}
)

# Install Python module to site-packages (makes it importable as a Python package)
install(
  DIRECTORY maurice_cam/
  DESTINATION lib/${PYTHON_VERSION}/site-packages/maurice_cam
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