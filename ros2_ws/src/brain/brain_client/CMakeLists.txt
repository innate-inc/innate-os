cmake_minimum_required(VERSION 3.10)
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

install(FILES
  brain_client/perception/yolov8n-pose.onnx
  DESTINATION "${PYTHON_INSTALL_DIR}/${PROJECT_NAME}/perception"
)

# Public authoring namespace for skill files (`from innate import Skill, Mobility, ...`)
ament_python_install_package(innate
  PACKAGE_DIR innate
)

# Install Python executables
install(PROGRAMS
  brain_client/nodes/brain_client_node.py
  brain_client/nodes/skills_server.py
  brain_client/nodes/input_manager.py
  brain_client/nodes/arm_sdk_server.py
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

  # Boot smoke with ROS domain ID isolation: proves the installed nodes
  # actually launch (exec bits, imports, entry points) under the current
  # install mode. The agent's behavior itself is covered by the headless
  # pytest suites in test/.
  add_launch_test(test/test_node_boot.launch.py
    RUNNER "${ament_cmake_ros_DIR}/run_test_isolated.py"
    TIMEOUT 240
  )
endif()

ament_package()
