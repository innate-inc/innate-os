################################################################################
# Set minimum required version of cmake, project name and compile options
################################################################################
cmake_minimum_required(VERSION 3.5)
project(ld08_driver)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

if(MSVC)
  add_compile_definitions(
    _USE_MATH_DEFINES
  )
endif()

################################################################################
# Find ament packages and libraries for ament and system dependencies
################################################################################
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(Boost REQUIRED system)
if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

################################################################################
# Build
################################################################################
include_directories(
  include
  ${catkin_INCLUDE_DIRS}
)

SET(CMAKE_CXX_FLAGS "-std=c++17 ${CMAKE_CXX_FLAGS}")

file(GLOB  MAIN_SRC src/*.cpp)

add_executable(ld08_driver  ${MAIN_SRC})
target_link_libraries(ld08_driver pthread udev  ${catkin_LIBRARIES})
ament_target_dependencies(ld08_driver
  rclcpp
  std_msgs
  sensor_msgs
)

################################################################################
# Install
################################################################################
install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

install(
  TARGETS ld08_driver
  RUNTIME DESTINATION lib/${PROJECT_NAME}
)

################################################################################
# Macro for ament package
################################################################################
ament_export_dependencies(rclcpp)
ament_export_dependencies(std_msgs)
ament_export_dependencies(sensor_msgs)
ament_export_include_directories(include)
ament_package()
