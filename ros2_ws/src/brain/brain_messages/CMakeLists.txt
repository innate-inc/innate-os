cmake_minimum_required(VERSION 3.8)
project(brain_messages)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(std_msgs REQUIRED)

# List your service and message files
set(srv_files
  "srv/ManipulationTask.srv"
  "srv/GetChatHistory.srv"
  "srv/GetTaskMetadataList.srv"
  "srv/GetAvailableDirectives.srv"
  "srv/GetTaskMetadata.srv"
  "srv/UpdateTaskMetadata.srv"
  "srv/ResetBrain.srv"
  "srv/GetAvailableBehaviors.srv"
  "srv/GetAvailableSkills.srv"
  "srv/ChangeMap.srv"
  "srv/ChangeNavigationMode.srv"
  "srv/SaveMap.srv"
  "srv/DeleteMap.srv"
  "srv/GetDatasetInfo.srv"
  "srv/SetDirectiveOnStartup.srv"
  "srv/LoadEpisode.srv"
  "srv/SubmitTrainingJob.srv"
  "srv/GetTrainingJobStatus.srv"
  "srv/GetTrainingJobStatusByName.srv"
  "srv/ListTrainingJobs.srv"
  "srv/DownloadTrainingModel.srv"
  "srv/ReloadSkillsAgents.srv"
)

set(msg_files
  "msg/RecorderStatus.msg"
  "msg/ReplayStatus.msg"
)

set(action_files
  "action/ExecuteSkill.action"
  "action/ExecutePolicy.action"
  "action/ExecuteBehavior.action"
)

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

# Generate ROS 2 interfaces
rosidl_generate_interfaces(${PROJECT_NAME}
  ${srv_files}
  ${msg_files}
  ${action_files}
  DEPENDENCIES builtin_interfaces std_msgs
)

ament_package()
