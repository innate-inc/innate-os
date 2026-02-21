cmake_minimum_required(VERSION 3.8)
project(brain_client)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)
find_package(std_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(brain_messages REQUIRED)


# Install Python modules
ament_python_install_package(${PROJECT_NAME}
  PACKAGE_DIR brain_client
)

# Install Python executables
install(PROGRAMS
  brain_client/brain_client_node.py
  brain_client/ws_client_node.py
  brain_client/skills_action_server.py
  brain_client/input_manager_node.py
  DESTINATION lib/${PROJECT_NAME}
)

# Install the launch folder
install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()

  find_package(ament_cmake_ros REQUIRED)
  find_package(launch_testing_ament_cmake REQUIRED)

  # Register integration tests with ROS domain ID isolation
  function(add_ros_isolated_launch_test path)
    set(RUNNER "${ament_cmake_ros_DIR}/run_test_isolated.py")
    add_launch_test("${path}" RUNNER "${RUNNER}" ${ARGN})
  endfunction()

  add_ros_isolated_launch_test(test/test_skill_integration.launch.py)
  add_ros_isolated_launch_test(test/test_pose_image.launch.py
    TIMEOUT 180
  )
endif()

ament_package()