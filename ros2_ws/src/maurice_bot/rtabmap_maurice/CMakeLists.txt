cmake_minimum_required(VERSION 3.8)
project(rtabmap_maurice)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)
find_package(rtabmap_slam REQUIRED)
find_package(rtabmap_viz REQUIRED)
find_package(rtabmap_util REQUIRED)

ament_python_install_package(${PROJECT_NAME})

install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
