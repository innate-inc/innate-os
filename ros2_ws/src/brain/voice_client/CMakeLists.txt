cmake_minimum_required(VERSION 3.8)
project(voice_client)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)


# Install Python modules
ament_python_install_package(${PROJECT_NAME}
  PACKAGE_DIR voice_client
)

# Install Python executables
install(PROGRAMS
  voice_client/voice_client_node.py
  DESTINATION lib/${PROJECT_NAME}
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


