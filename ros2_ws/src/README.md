# ROS 2 Workspace Source

This is the source tree for the Innate OS ROS 2 workspace. Packages are grouped
into three subsystems: **`brain/`** (cloud agent + manipulation), **`cloud/`**
(cloud connectivity, logging, training), and **`mars_bot/`** (the robot's
drivers, control, navigation, and simulation).

> Build from the workspace root: `cd ros2_ws && colcon build`.
> External dependencies are pulled via [`dependencies.repos`](dependencies.repos)
> (`vcs import src < src/dependencies.repos`).

## brain/ — Agent bridge & manipulation
| Package | Description |
| --- | --- |
| [`brain_client`](brain/brain_client) | Bridges to a cloud agent via WebSocket. |
| [`brain_messages`](brain/brain_messages) | Message, service, and action definitions for the brain system. |
| [`manipulation`](brain/manipulation) | Manipulation, behavior recording, and policy execution. |

## cloud/ — Cloud connectivity, logging & training
| Package | Description |
| --- | --- |
| [`clients`](cloud/clients) | Cloud client libraries: auth, proxy, and training. |
| [`innate_cloud_msgs`](cloud/innate_cloud_msgs) | Message/service definitions for cloud training. |
| [`innate_logger`](cloud/innate_logger) | Logs robot vitals, directives, and chat to the cloud. |
| [`innate_training_node`](cloud/innate_training_node) | Training job management, status publishing, and file transfer. |
| [`innate_uninavid`](cloud/innate_uninavid) | Bridges compressed camera images to a cloud websocket and publishes `cmd_vel` in response. |

## mars_bot/ — Robot drivers, control, nav & sim
| Package | Description |
| --- | --- |
| [`mars_arm`](mars_bot/mars_arm) | Camera driver and arm control. |
| [`mars_bringup`](mars_bot/mars_bringup) | Bringup scripts and configurations for the physical robot. |
| [`mars_bt_provisioner`](mars_bot/mars_bt_provisioner) | Bluetooth provisioning service. |
| [`mars_cam`](mars_bot/mars_cam) | Camera driver with GStreamer, OpenCV, and WebRTC streaming. |
| [`mars_control`](mars_bot/mars_control) | Control interface (joystick, keyboard, leader arm). |
| [`mars_msgs`](mars_bot/mars_msgs) | Message definitions for the robot. |
| [`mars_nav`](mars_bot/mars_nav) | Python-based navigation package. |
| [`mars_description`](mars_bot/mars_description) | Robot model assets (URDF, SRDF, meshes) shared with the arm/IK stack. |
| [`mars_sim_bringup`](mars_bot/mars_sim_bringup) | Starts rosbridge for simulation. |
