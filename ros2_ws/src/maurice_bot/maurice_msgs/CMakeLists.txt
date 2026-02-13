cmake_minimum_required(VERSION 3.8)
project(maurice_msgs)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(action_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(unique_identifier_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  # comment the line when a copyright and license is added to all source files
  set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips cpplint (only works in a git repo)
  # comment the line when this package is in a git repo and when
  # a copyright and license is added to all source files
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/ArmStatus.msg"
  "srv/LightCommand.srv"
  "srv/GotoJS.srv"
  "srv/GotoJSTrajectory.srv"
  "srv/SetRobotName.srv"
  "srv/GridLocalize.srv"
  "srv/TriggerUpdate.srv"
  "srv/Shutdown.srv"
  DEPENDENCIES
    std_msgs
    std_srvs
    geometry_msgs
    sensor_msgs
    nav_msgs
    action_msgs
    builtin_interfaces
    unique_identifier_msgs
)

ament_package()
