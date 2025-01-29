cmake_minimum_required(VERSION 3.8)
project(maurice_sim_bringup)

# Use standard ament
find_package(ament_cmake REQUIRED)

# No actual code to compile. We just have a launch file to install.
install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

# Install Python modules
ament_python_install_package(${PROJECT_NAME}
  PACKAGE_DIR maurice_sim_bringup
)

# Install Python executables
install(PROGRAMS
  maurice_sim_bringup/odom_tf_broadcaster.py
  DESTINATION lib/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
