cmake_minimum_required(VERSION 3.8)
project(innate_cloud_msgs)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(action_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/TrainingParams.msg"
  "msg/TransferProgress.msg"
  "msg/TrainingRunStatus.msg"
  "msg/TrainingSkillStatus.msg"
  "msg/TrainingJobList.msg"
  "srv/SubmitSkill.srv"
  "srv/CreateRun.srv"
  "srv/DownloadResults.srv"
  "srv/GetTrainingStatus.srv"
  "action/NavigateInstruction.action"
  DEPENDENCIES builtin_interfaces action_msgs
)

ament_package()
