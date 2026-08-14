cmake_minimum_required(VERSION 3.10)
project(mars_bt_provisioner)

# Use standard ament
find_package(ament_cmake REQUIRED)

# No actual code to compile. We just have a launch file to install.
install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

# Install Python modules
ament_python_install_package(${PROJECT_NAME}
  PACKAGE_DIR mars_bt_provisioner
)

# Install Python executables
install(PROGRAMS
  mars_bt_provisioner/simple_bt_service.py
  DESTINATION lib/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()

  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(test_command_layer test/test_command_layer.py
    APPEND_ENV PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/mars_bt_provisioner
  )
  ament_add_pytest_test(test_identity_applier test/test_identity_applier.py)
endif()

ament_package()
