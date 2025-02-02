cmake_minimum_required(VERSION 3.8)
project(brain_messages)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

# List your service (and/or message) files.
set(srv_files
  "srv/GetChatHistory.srv"
)

# Generate the interfaces
rosidl_generate_interfaces(${PROJECT_NAME}
  ${srv_files}
  DEPENDENCIES std_msgs
)

ament_package() 