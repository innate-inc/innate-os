# Innate-OS ROS2 Dependencies Analysis

This document provides a comprehensive list of all dependencies found in the `ros2_ws/src/` workspace.
Generated from analysis of package.xml, CMakeLists.txt, Python imports, and launch files.

---

## Quick Reference: Missing Packages to Install

Based on this analysis, ensure these are in `apt-dependencies.txt`:

```bash
# Core ROS2 (should already be installed with ros-humble-ros-base)
ros-humble-rclcpp
ros-humble-rclpy

# Navigation
ros-humble-nav2-simple-commander
ros-humble-nav2-map-server
ros-humble-nav2-bringup
ros-humble-navigation2

# Transforms
ros-humble-tf2
ros-humble-tf2-ros
ros-humble-tf2-geometry-msgs

# Vision/Perception
ros-humble-cv-bridge
ros-humble-camera-info-manager
ros-humble-image-transport

# MoveIt (if using arm)
ros-humble-moveit
ros-humble-moveit-py
ros-humble-moveit-ros-move-group

# Rosbridge
ros-humble-rosbridge-suite
ros-humble-rosbridge-server

# Rosbag
ros-humble-rosbag2

# Messages
ros-humble-diagnostic-msgs
ros-humble-lifecycle-msgs

# DepthAI (OAK cameras)
ros-humble-depthai-ros

# Topic tools (commonly missing!)
ros-humble-topic-tools

# KDL Parser
ros-humble-kdl-parser-py

# GStreamer (for WebRTC)
gstreamer1.0-tools
gstreamer1.0-plugins-base
gstreamer1.0-plugins-good
gstreamer1.0-plugins-bad
gstreamer1.0-plugins-ugly
gstreamer1.0-nice
python3-gst-1.0
```

---

## 1. ROS2 Core Packages

| Package | Apt Package | Found In | Usage |
|---------|-------------|----------|-------|
| rclpy | ros-humble-rclpy | All Python packages | Python ROS2 client library |
| rclcpp | ros-humble-rclcpp | maurice_control, maurice_head, maurice_bringup, stage_ros2 | C++ ROS2 client library |
| rclcpp_components | ros-humble-rclcpp-components | stage_ros2 | C++ component architecture |
| ament_cmake | ros-humble-ament-cmake | All packages | Build system |
| ament_cmake_python | ros-humble-ament-cmake-python | maurice_arm, maurice_bringup, maurice_control, maurice_head | Python support in CMake |
| ament_index_python | ros-humble-ament-index-python | maurice_sim, maurice_nav, maurice_control, manipulation | Package indexing |
| launch | ros-humble-launch | All packages | ROS2 launch system |
| launch_ros | ros-humble-launch-ros | All packages | ROS2 launch utilities |

**Locations:**
- `maurice_bot/maurice_nav/package.xml`
- `maurice_bot/maurice_bringup/package.xml`
- `maurice_bot/maurice_control/package.xml`

---

## 2. ROS2 Message & Service Packages

| Package | Apt Package | Found In | Usage |
|---------|-------------|----------|-------|
| std_msgs | ros-humble-std-msgs | All packages | Standard message types |
| std_srvs | ros-humble-std-srvs | Most packages | Standard service definitions |
| geometry_msgs | ros-humble-geometry-msgs | All packages | Pose, Twist, Transform messages |
| sensor_msgs | ros-humble-sensor-msgs | Most packages | Image, JointState, LaserScan |
| nav_msgs | ros-humble-nav-msgs | Navigation packages | Odometry, Path, OccupancyGrid |
| action_msgs | ros-humble-action-msgs | maurice_msgs, brain_messages | Action framework messages |
| builtin_interfaces | ros-humble-builtin-interfaces | maurice_msgs, brain_messages | Built-in type definitions |
| unique_identifier_msgs | ros-humble-unique-identifier-msgs | maurice_msgs | UUID message type |
| diagnostic_msgs | ros-humble-diagnostic-msgs | maurice_log | Diagnostic system messages |
| lifecycle_msgs | ros-humble-lifecycle-msgs | maurice_nav | Lifecycle management |
| rosgraph_msgs | ros-humble-rosgraph-msgs | stage_ros2 | ROS graph messages |
| rcl_interfaces | ros-humble-rcl-interfaces | maurice_arm, maurice_head | Parameter services |

**Locations:**
- `maurice_bot/maurice_msgs/CMakeLists.txt`
- `brain/brain_messages/package.xml`

---

## 3. Navigation & Nav2 Packages

| Package | Apt Package | Found In | Usage |
|---------|-------------|----------|-------|
| nav2_simple_commander | ros-humble-nav2-simple-commander | maurice_nav | High-level navigation API |
| nav2_map_server | ros-humble-nav2-map-server | maurice_nav | Map management service |
| tf2 | ros-humble-tf2 | maurice_bringup, stage_ros2 | Transform library core |
| tf2_ros | ros-humble-tf2-ros | All packages | ROS2 transform library |
| tf2_geometry_msgs | ros-humble-tf2-geometry-msgs | maurice_bringup, stage_ros2 | TF2 geometry conversions |

**Code Example:**
```python
# maurice_nav/mode_manager.py
from nav2_simple_commander.robot_navigator import BasicNavigator
import tf2_ros
from geometry_msgs.msg import Pose, TransformStamped
from nav_msgs.msg import Odometry
```

**Locations:**
- `maurice_bot/maurice_nav/launch/navigation.launch.py`
- `maurice_bot/maurice_nav/launch/mapping.launch.py`
- `maurice_bot/maurice_nav/package.xml`

---

## 4. Vision & Perception Packages

| Package | Apt Package | Found In | Usage |
|---------|-------------|----------|-------|
| cv_bridge | ros-humble-cv-bridge | maurice_arm, maurice_bringup, manipulation, innate_webrtc_streamer | OpenCV-ROS2 bridge |
| camera_info_manager | ros-humble-camera-info-manager | maurice_bringup | Camera calibration |
| image_transport | ros-humble-image-transport | maurice_bringup | Image transport plugins |

**C++ Includes:**
```cpp
// maurice_arm/camera.cpp & maurice_bringup/camera.cpp
#include <cv_bridge/cv_bridge.h>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
```

**Python Imports:**
```python
# manipulation/behavior_server.py
from cv_bridge import CvBridge
```

**Locations:**
- `maurice_bot/maurice_bringup/CMakeLists.txt`
- `maurice_bot/maurice_arm/CMakeLists.txt`

---

## 5. Hardware & Manipulation Packages

| Package | Apt Package | Found In | Usage |
|---------|-------------|----------|-------|
| moveit_py | ros-humble-moveit-py | maurice_arm | MoveIt2 Python API |
| moveit_ros_move_group | ros-humble-moveit-ros-move-group | maurice_arm | MoveIt2 move group |
| kdl_parser_py | ros-humble-kdl-parser-py | maurice_arm | KDL parser for Python |

**Locations:**
- `maurice_bot/maurice_arm/package.xml`

---

## 6. Middleware & Bridge Packages

| Package | Apt Package | Found In | Usage |
|---------|-------------|----------|-------|
| rosbridge_server | ros-humble-rosbridge-server | maurice_control, maurice_sim_bringup | WebSocket bridge |
| rosbridge_suite | ros-humble-rosbridge-suite | maurice_sim_bringup | Full rosbridge |
| rosbag2_py | ros-humble-rosbag2 | maurice_bringup | Bag recording/playback |
| rosidl_default_generators | ros-humble-rosidl-default-generators | maurice_msgs, brain_messages | IDL code generator |
| rosidl_default_runtime | ros-humble-rosidl-default-runtime | maurice_msgs, brain_messages | IDL runtime |

**Locations:**
- `maurice_bot/maurice_control/package.xml`
- `maurice_bot/maurice_bringup/package.xml`
- `brain/brain_messages/package.xml`

---

## 7. External Hardware SDKs

| Package | Apt Package | Found In | Usage |
|---------|-------------|----------|-------|
| depthai | ros-humble-depthai-ros | maurice_bringup | OAK-D depth camera SDK |
| depthai_bridge | ros-humble-depthai-ros | maurice_bringup | OAK-D to ROS2 bridge |
| python3-dynamixel-sdk | python3-dynamixel-sdk | maurice_arm | Dynamixel servo SDK |
| libopencv-dev | libopencv-dev | maurice_arm, maurice_bringup | OpenCV |
| nlohmann-json-dev | nlohmann-json3-dev | maurice_control | JSON for C++ |
| stage | ros-humble-stage-ros | stage_ros2 | 2D robot simulator |

**C++ Includes:**
```cpp
// maurice_control/app.cpp
#include <nlohmann/json.hpp>

// maurice_bringup/camera.cpp
#include "depthai/device/Device.hpp"
#include "depthai/pipeline/Pipeline.hpp"
#include "depthai_bridge/BridgePublisher.hpp"
```

**Locations:**
- `maurice_bot/maurice_bringup/CMakeLists.txt`
- `maurice_bot/maurice_control/package.xml`

---

## 8. Python Packages (Non-ROS)

These should be in `pip-requirements.txt`:

| Package | Pip Package | Found In | Usage |
|---------|-------------|----------|-------|
| numpy | numpy | behavior_server, visualizer, recorder, control | Numerical computing |
| opencv-python | opencv-python | brain_client, behavior_server, manipulation | Computer vision |
| torch | torch | behavior_server | Deep learning (PyTorch) |
| h5py | h5py | primitive_loader, visualizer, behavior_server | HDF5 file support |
| pydantic | pydantic | brain_client/message_types.py | Data validation |
| websockets | websockets | ws_client_node.py | WebSocket client |
| cartesia | cartesia | tts_handler.py | Text-to-speech API |
| pynput | pynput | keyboard.py | Keyboard input |
| pygame | pygame | joystick.py | Game controller input |
| dynamixel-sdk | dynamixel-sdk | dynamixel.py, servo_manager.py | Motor control |

**Python Imports:**
```python
# innate_webrtc_streamer/webrtc_streamer.py
import gi
from gi.repository import Gst, GstWebRTC, GstSdp
import numpy as np

# behavior_server.py
import torch
import h5py

# brain_client/message_types.py
from pydantic import BaseModel

# ws_client_node.py
import websockets

# joystick.py
import pygame
from pynput import keyboard
```

**Locations:**
- `brain/brain_client/brain_client/brain_client_node.py`
- `brain/manipulation/manipulation/behavior_server.py`
- `brain/innate_webrtc_streamer/innate_webrtc_streamer/webrtc_streamer.py`

---

## 9. GStreamer Packages (for WebRTC)

Required by `innate_webrtc_streamer`:

| Apt Package | Usage |
|-------------|-------|
| gstreamer1.0-tools | GStreamer tools |
| gstreamer1.0-plugins-base | Base plugins |
| gstreamer1.0-plugins-good | Good plugins |
| gstreamer1.0-plugins-bad | Bad plugins (includes webrtc) |
| gstreamer1.0-plugins-ugly | Ugly plugins |
| gstreamer1.0-libav | FFmpeg plugin |
| gstreamer1.0-nice | ICE/STUN/TURN support |
| libgstreamer1.0-dev | Development files |
| libgstreamer-plugins-base1.0-dev | Base plugins dev |
| gir1.2-gstreamer-1.0 | GObject introspection |
| gir1.2-gst-plugins-base-1.0 | GObject introspection |
| python3-gi | PyGObject |
| python3-gst-1.0 | GStreamer Python bindings |

**Locations:**
- `brain/innate_webrtc_streamer/innate_webrtc_streamer/webrtc_streamer.py`

---

## 10. Testing & Linting Packages

| Package | Apt Package | Found In |
|---------|-------------|----------|
| ament_lint_auto | ros-humble-ament-lint-auto | All packages |
| ament_lint_common | ros-humble-ament-lint-common | All packages |
| ament_copyright | ros-humble-ament-copyright | innate_webrtc_streamer |
| ament_flake8 | ros-humble-ament-flake8 | innate_webrtc_streamer |
| ament_pep257 | ros-humble-ament-pep257 | innate_webrtc_streamer |
| python3-pytest | python3-pytest | brain_client, innate_webrtc_streamer |

**Locations:**
- `maurice_bot/stage_ros2/package.xml`
- `brain/brain_client/package.xml`

---

## 11. VCS Dependencies (External Repos)

These are cloned via `vcs import` from `dependencies.repos`:

| Repository | Branch | Usage |
|------------|--------|-------|
| [rplidar_ros](https://github.com/Slamtec/rplidar_ros) | ros2 | RPLidar laser scanner driver |

**Location:** `ros2_ws/src/dependencies.repos`

---

## Internal Workspace Packages

These are packages within this workspace (no external install needed):

| Package | Path | Description |
|---------|------|-------------|
| maurice_msgs | maurice_bot/maurice_msgs | Custom messages for Maurice |
| brain_messages | brain/brain_messages | Custom messages for Brain system |
| maurice_arm | maurice_bot/maurice_arm | Arm control and IK |
| maurice_nav | maurice_bot/maurice_nav | Navigation stack |
| maurice_sim | maurice_bot/maurice_sim | Simulation support |
| maurice_sim_bringup | maurice_bot/maurice_sim_bringup | Simulation launch |
| maurice_head | maurice_bot/maurice_head | Head control |
| maurice_bringup | maurice_bot/maurice_bringup | Robot bringup |
| maurice_log | maurice_bot/maurice_log | Logging |
| maurice_control | maurice_bot/maurice_control | Control system |
| maurice_bt_provisioner | maurice_bot/maurice_bt_provisioner | Bluetooth provisioner |
| stage_ros2 | maurice_bot/stage_ros2 | Stage simulator ROS2 wrapper |
| brain_client | brain/brain_client | Brain system client |
| manipulation | brain/manipulation | Manipulation behaviors |
| innate_webrtc_streamer | brain/innate_webrtc_streamer | WebRTC streaming |

---

## Summary Statistics

| Category | Count |
|----------|-------|
| ROS2 Core Packages | 8 |
| ROS2 Message/Service Packages | 12 |
| Navigation Packages | 5 |
| Perception Packages | 3 |
| Hardware/Manipulation Packages | 3 |
| Bridge/Middleware Packages | 5 |
| External SDKs | 7 |
| Python Packages (non-ROS) | 10 |
| GStreamer Packages | 13 |
| Testing Packages | 6 |
| **Total External Dependencies** | **~72** |
| Internal Workspace Packages | 15 |

---

## Commonly Missing Packages

If you see errors about missing packages, try installing:

```bash
# topic_tools (often missing)
sudo apt install ros-humble-topic-tools

# Nav2 full stack
sudo apt install ros-humble-navigation2 ros-humble-nav2-bringup

# TF2
sudo apt install ros-humble-tf2-ros ros-humble-tf2-geometry-msgs

# Image transport
sudo apt install ros-humble-image-transport ros-humble-image-transport-plugins

# MoveIt
sudo apt install ros-humble-moveit

# Rosbridge
sudo apt install ros-humble-rosbridge-suite

# DepthAI
sudo apt install ros-humble-depthai-ros
```
